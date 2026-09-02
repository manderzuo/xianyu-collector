# -*- coding: utf-8 -*-
"""全 API 统一响应格式。"""
from typing import Any


def ok(data: Any = None, message: str = "操作成功", code: str = "ok") -> dict[str, Any]:
    return {"success": True, "code": code, "message": message, "data": data}


def error(message: str, code: str = "error", data: Any = None) -> dict[str, Any]:
    return {"success": False, "code": code, "message": message, "data": data}
