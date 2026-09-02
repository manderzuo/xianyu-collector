"""Redis 连接辅助。

调度任务需要一个轻量的跨进程状态存储（例如平台日切换和任务锁）。
统一从配置创建连接，避免各服务自行拼接 Redis 参数。
"""
from __future__ import annotations

from redis.asyncio import Redis

from common.config import settings


def get_redis_client() -> Redis:
    """返回一个使用当前部署配置的异步 Redis 客户端。"""
    return Redis.from_url(settings.redis_url, decode_responses=True)


__all__ = ["get_redis_client"]
