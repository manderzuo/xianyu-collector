# -*- coding: utf-8 -*-
"""集中式环境变量配置。

所有服务(backend / websocket / scheduler)共用本配置,
环境变量命名与 docker-compose.yml 保持一致(MYSQL_* / REDIS_* 等)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get_int(name: str, default: int) -> int:
    """读取整数环境变量,非法值回退默认。"""
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """运行配置。默认值用于本地开发,生产由环境变量覆盖。"""

    # 运行环境: development / production / test
    environment: str = "development"

    # MySQL
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "xianyu"
    mysql_password: str = "xy_rewrite_2026"
    mysql_database: str = "xianyu_rewrite"
    mysql_charset: str = "utf8mb4"

    # Redis
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0

    # JWT
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    # 服务端口
    backend_web_port: int = 8089
    websocket_port: int = 8090
    scheduler_port: int = 8091

    # 跨域
    cors_origins: str = "*"

    # 服务间地址与文件目录
    backend_service_url: str = "http://127.0.0.1:8089"
    websocket_service_url: str = "http://127.0.0.1:8090"
    scheduler_service_url: str = "http://127.0.0.1:8091"
    static_dir: str = "./static"
    backup_dir: str = "./backups"
    browser_data_dir: str = "./browser_data"
    browser_headless: bool = True

    # 品牌占位：业务代码只从这里读取，后续可集中替换
    brand_name: str = "BRAND_NAME"
    brand_domain: str = ""

    # 闲鱼登录接口。默认值与旧版登录协议一致，生产环境可通过环境变量覆盖。
    goofish_passport_host: str = "https://passport.goofish.com"
    goofish_mtop_host: str = "https://h5api.m.goofish.com"
    goofish_proxy: str = ""
    amap_web_key: str = ""

    # 分销卡券上游代理及外部密钥管理鉴权。仅从部署环境读取，未配置时
    # 相关接口返回明确的配置错误，不伪造货源或提货成功。
    card_dock_base_url: str = ""
    external_api_key: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构建配置实例。"""
        return cls(
            environment=os.getenv("ENVIRONMENT", "development"),
            mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            mysql_port=_get_int("MYSQL_PORT", 3306),
            mysql_user=os.getenv("MYSQL_USER", "xianyu"),
            mysql_password=os.getenv("MYSQL_PASSWORD", "xy_rewrite_2026"),
            mysql_database=os.getenv("MYSQL_DATABASE", "xianyu_rewrite"),
            mysql_charset=os.getenv("MYSQL_CHARSET", "utf8mb4"),
            redis_host=os.getenv("REDIS_HOST", "127.0.0.1"),
            redis_port=_get_int("REDIS_PORT", 6379),
            redis_password=os.getenv("REDIS_PASSWORD", ""),
            redis_db=_get_int("REDIS_DB", 0),
            jwt_secret=os.getenv("JWT_SECRET", "change-me-in-production"),
            jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
            jwt_expire_minutes=_get_int("JWT_EXPIRE_MINUTES", 1440),
            backend_web_port=_get_int("BACKEND_WEB_PORT", 8089),
            websocket_port=_get_int("WEBSOCKET_PORT", 8090),
            scheduler_port=_get_int("SCHEDULER_PORT", 8091),
            cors_origins=os.getenv("CORS_ORIGINS", "*"),
            backend_service_url=os.getenv("BACKEND_SERVICE_URL", "http://127.0.0.1:8089"),
            websocket_service_url=os.getenv("WEBSOCKET_SERVICE_URL", "http://127.0.0.1:8090"),
            scheduler_service_url=os.getenv("SCHEDULER_SERVICE_URL", "http://127.0.0.1:8091"),
            static_dir=os.getenv("STATIC_DIR", "./static"),
            backup_dir=os.getenv("BACKUP_DIR", "./backups"),
            browser_data_dir=os.getenv("BROWSER_DATA_DIR", "./browser_data"),
            browser_headless=os.getenv("BROWSER_HEADLESS", "true").lower() != "false",
            brand_name=os.getenv("BRAND_NAME", "BRAND_NAME"),
            brand_domain=os.getenv("BRAND_DOMAIN", ""),
            goofish_passport_host=os.getenv("GOOFISH_PASSPORT_HOST", "https://passport.goofish.com").rstrip("/"),
            goofish_mtop_host=os.getenv("GOOFISH_MTOP_HOST", "https://h5api.m.goofish.com").rstrip("/"),
            goofish_proxy=os.getenv("GOOFISH_PROXY", "").strip(),
            # 高德 Web 服务 Key 只用于商品所在地输入提示；可用环境变量覆盖。
            amap_web_key=os.getenv("AMAP_WEB_KEY", "c9b68d4ce9a2a97f22a4a439404488ca").strip(),
            card_dock_base_url=os.getenv("CARD_DOCK_BASE_URL", "").strip().rstrip("/"),
            external_api_key=os.getenv("EXTERNAL_API_KEY", "").strip(),
        )

    @property
    def mysql_dsn(self) -> str:
        """SQLAlchemy 异步连接串(mysql+asyncmy)。"""
        return (
            f"mysql+asyncmy://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset={self.mysql_charset}"
        )

    @property
    def redis_url(self) -> str:
        """Redis 连接串(可选密码)。"""
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"


# 模块级单例,进程启动时读取一次环境变量
settings = Settings.from_env()
