# -*- coding: utf-8 -*-
"""WebSocket 连接生命周期管理。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import WebSocket


@dataclass
class ClientConnection:
    socket: WebSocket
    connection_id: str = field(default_factory=lambda: uuid4().hex)
    account_id: str | None = None
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ConnectionManager:
    """按连接 ID 管理客户端，并提供广播/安全发送能力。"""

    def __init__(self) -> None:
        self._clients: dict[str, ClientConnection] = {}
        self._lock = asyncio.Lock()

    async def connect(self, socket: WebSocket, account_id: str | None = None) -> ClientConnection:
        await socket.accept()
        client = ClientConnection(socket=socket, account_id=account_id)
        async with self._lock:
            self._clients[client.connection_id] = client
        return client

    async def disconnect(self, connection_id: str) -> None:
        async with self._lock:
            self._clients.pop(connection_id, None)

    async def send(self, client: ClientConnection, payload: dict[str, Any]) -> None:
        await client.socket.send_json(payload)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients.values())
        await asyncio.gather(*(self.send(client, payload) for client in clients), return_exceptions=True)

    @property
    def size(self) -> int:
        return len(self._clients)
