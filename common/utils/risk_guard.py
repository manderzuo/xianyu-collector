# -*- coding: utf-8 -*-
"""防风控限速器(自主实现,简单版)。

设计思路(仅参考通用做法,代码为本仓库独立实现):
- 随机抖动:请求间隔在 [min_gap, max_gap] 内随机,避免固定节奏被识别为机器;
- 滑动窗口:单位时间窗口内请求数封顶,防止突发打太猛;
- 指数退避:命中风控后冷却时间随次数翻倍,并带上限;
- 可选持久化:冷却状态写入 JSON 文件,进程重启后可恢复。

用法::

    from common.utils import RiskGuard, RiskPolicy

    guard = RiskGuard("search", RiskPolicy(min_gap=3.0, max_gap=5.0))
    guard.wait()          # 每次请求前调用
    guard.on_success()    # 请求成功后调用
    guard.on_risk()       # 触发风控时调用
    guard.on_error()      # 网络异常时调用
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RiskPolicy:
    """限速策略参数。"""

    min_gap: float = 3.0          # 最短间隔(秒)
    max_gap: float = 5.0          # 最长间隔(秒,随机抖动上界)
    window: float = 60.0          # 滑动窗口长度(秒)
    max_per_window: int = 15      # 窗口内最大请求数
    cooldown: float = 90.0        # 基础冷却(秒)
    max_cooldown: float = 900.0   # 冷却上限(秒)

    @classmethod
    def gentle(cls) -> "RiskPolicy":
        """更保守的策略:间隔更慢、窗口更松、冷却更长。"""
        return cls(
            min_gap=6.0,
            max_gap=10.0,
            window=120.0,
            max_per_window=8,
            cooldown=300.0,
            max_cooldown=1800.0,
        )


class RiskGuard:
    """线程安全的防风控限速器,每个接口一个实例。"""

    def __init__(
        self,
        name: str,
        policy: RiskPolicy | None = None,
        state_file: str | Path | None = None,
    ) -> None:
        self.name = name
        self.policy = policy or RiskPolicy()
        self.state_file = Path(state_file) if state_file else None

        self._last_call: float = 0.0
        self._recent_hits: list[float] = []
        self._cooldown_until: float = 0.0
        self._risk_streak: int = 0
        self._peak_rpm: int = 0
        self._lock = threading.Lock()

        if self.state_file:
            self._load_state()

    # ── 状态持久化(可选) ──────────────────────────
    def _load_state(self) -> None:
        """恢复上次冷却状态,避免重启后立刻猛打。"""
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            until = float(data.get("cooldown_until", 0))
            if until > time.time():
                self._cooldown_until = until
                self._risk_streak = int(data.get("risk_streak", 0))
                left = int(until - time.time())
                print(f"[RiskGuard:{self.name}] 冷却剩余 {left}s(持久化恢复)", flush=True)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    def _save_state(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.write_text(
                json.dumps(
                    {
                        "cooldown_until": self._cooldown_until,
                        "risk_streak": self._risk_streak,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ── 限速核心 ──────────────────────────────
    def wait(self) -> None:
        """请求前调用:冷却检查 -> 窗口限速 -> 随机抖动。"""
        with self._lock:
            now = time.time()

            # 1) 冷却
            if now < self._cooldown_until:
                left = self._cooldown_until - now
                print(f"[RiskGuard:{self.name}] 冷却中,再等 {int(left)}s", flush=True)
                time.sleep(left)
                now = time.time()

            # 2) 滑动窗口
            self._recent_hits = [t for t in self._recent_hits if t > now - self.policy.window]
            if len(self._recent_hits) >= self.policy.max_per_window:
                wait_s = self._recent_hits[0] + self.policy.window - now
                print(f"[RiskGuard:{self.name}] 窗口限速,等 {wait_s:.1f}s", flush=True)
                time.sleep(wait_s)
                now = time.time()
                self._recent_hits = [t for t in self._recent_hits if t > now - self.policy.window]

            # 3) 随机抖动
            gap = random.uniform(self.policy.min_gap, self.policy.max_gap)
            elapsed = now - self._last_call
            if self._last_call and elapsed < gap:
                time.sleep(gap - elapsed)
            self._last_call = time.time()

    def on_success(self) -> None:
        """请求成功:记录命中,统计峰值速率。"""
        with self._lock:
            now = time.time()
            self._recent_hits.append(now)
            recent = [t for t in self._recent_hits if t > now - 60.0]
            self._peak_rpm = max(self._peak_rpm, len(recent))
            if self._risk_streak:
                self._risk_streak = 0

    def on_risk(self, message: str = "") -> None:
        """命中风控:指数退避冷却并持久化。"""
        with self._lock:
            self._risk_streak += 1
            factor = 2 ** (self._risk_streak - 1)
            cooldown = min(self.policy.cooldown * factor, self.policy.max_cooldown)
            self._cooldown_until = time.time() + cooldown
            print(
                f"[RiskGuard:{self.name}] 风控 {message} 退避 {cooldown:.0f}s"
                f"(第{self._risk_streak}次)",
                flush=True,
            )
        self._save_state()
        time.sleep(cooldown)

    def on_error(self, message: str = "") -> None:
        """网络异常:小幅退避(不持久化)。"""
        with self._lock:
            self._risk_streak += 1
            cooldown = min(15.0 * (2 ** (self._risk_streak - 1)), 120.0)
            print(
                f"[RiskGuard:{self.name}] 网络异常 {message},等 {cooldown:.0f}s 重试",
                flush=True,
            )
        time.sleep(cooldown)

    def reset(self) -> None:
        """清空失败计数(如风控已解除)。"""
        with self._lock:
            self._risk_streak = 0
            self._cooldown_until = 0.0

    @property
    def peak_rpm(self) -> int:
        return self._peak_rpm

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until
