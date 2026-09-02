# -*- coding: utf-8 -*-
"""轻量代理池：健康度、轮换和冷却均在本地完成。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from itertools import cycle


@dataclass
class ProxyEndpoint:
    url: str
    weight: int = 1
    failures: int = 0
    cooldown_until: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return time.time() >= self.cooldown_until


class ProxyPool:
    def __init__(self, endpoints: list[str] | None = None) -> None:
        self._items = [ProxyEndpoint(url=item) for item in (endpoints or []) if item]
        self._cursor = cycle(self._items) if self._items else None

    def next(self) -> ProxyEndpoint | None:
        if not self._cursor:
            return None
        for _ in range(len(self._items)):
            item = next(self._cursor)
            if item.available:
                return item
        return None

    def report_failure(self, url: str, cooldown: float = 30.0) -> None:
        for item in self._items:
            if item.url == url:
                item.failures += 1
                item.cooldown_until = time.time() + cooldown * min(item.failures, 10)

    def report_success(self, url: str) -> None:
        for item in self._items:
            if item.url == url:
                item.failures = 0
                item.cooldown_until = 0.0
