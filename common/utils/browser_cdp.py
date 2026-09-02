# -*- coding: utf-8 -*-
"""可选浏览器 CDP 连接参数。

Playwright/Patchright 由需要发布或验证的服务按需安装和调用；共享层只负责
整理连接配置，确保没有浏览器进程或平台账号状态被隐式创建。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CdpTarget:
    endpoint: str
    browser_name: str = "chromium"
    headless: bool = True


def normalize_endpoint(endpoint: str, default_port: int = 9222) -> str:
    value = endpoint.strip()
    if not value:
        return f"http://127.0.0.1:{default_port}"
    if "://" not in value:
        return f"http://{value}"
    return value.rstrip("/")
