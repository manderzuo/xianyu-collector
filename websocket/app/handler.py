# -*- coding: utf-8 -*-
"""长连接消息处理与心跳协议。"""
from __future__ import annotations

from typing import Any
from fastapi import WebSocket, WebSocketDisconnect

from websocket.app.connection import ConnectionManager


async def handle_socket(socket: WebSocket, manager: ConnectionManager) -> None:
    account_id = socket.query_params.get("account_id")
    client = await manager.connect(socket, account_id=account_id)
    await manager.send(client, {"type": "connected", "connection_id": client.connection_id, "heartbeat": "ping/pong"})
    try:
        while True:
            message: dict[str, Any] = await socket.receive_json()
            message_type = message.get("type", "message")
            if message_type in {"ping", "heartbeat"}:
                await manager.send(client, {"type": "pong", "ts": message.get("ts")})
            elif message_type == "subscribe":
                await manager.send(client, {"type": "subscribed", "channels": message.get("channels", [])})
            else:
                # 统一事件回执，实际消息分发器可在此接入 Redis stream。
                await manager.send(client, {"type": "ack", "event": message_type, "data": message.get("data")})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(client.connection_id)
