# -*- coding: utf-8 -*-
"""调度中心共享任务目录。"""

TASK_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("refresh_cookies", "Cookie 续期", "*/20 * * * *"),
    ("refresh_tokens", "Token 续期", "*/30 * * * *"),
    ("sync_messages", "同步消息", "*/2 * * * *"),
    ("auto_reply", "自动回复", "*/1 * * * *"),
    ("sync_orders", "拉取订单", "*/5 * * * *"),
    ("sync_products", "同步商品", "*/10 * * * *"),
    ("redelivery", "定时补发货", "*/5 * * * *"),
    ("day_switch", "平台日切换", "*/1 * * * *"),
    ("cleanup_browser_data", "清理禁用账号浏览器数据", "*/10 * * * *"),
    ("close_notice", "关闭账号消息通知", "*/10 * * * *"),
    ("red_flower", "自动求小红花", "*/5 * * * *"),
    ("seller_fill", "采集商品卖家ID补全", "*/1 * * * *"),
    ("dm_send", "采集商品发送私信", "*/1 * * * *"),
    ("auto_order", "采集商品自动下单", "*/1 * * * *"),
    ("publish_retry", "重试发布", "*/10 * * * *"),
    ("refresh_listings", "擦亮商品", "0 */6 * * *"),
    ("auto_rate", "自动评价", "*/15 * * * *"),
    ("refund_timeout", "退款超时检测", "*/10 * * * *"),
    ("shipment_timeout", "发货超时检测", "*/10 * * * *"),
    ("crawl_goofish", "采集商品", "*/15 * * * *"),
    ("crawl_supply", "采集货源", "*/30 * * * *"),
    ("monitor_listings", "选品监控", "*/10 * * * *"),
    ("monitor_price", "价格监控", "*/15 * * * *"),
    ("distribution_sync", "分销同步", "*/20 * * * *"),
    ("send_notifications", "发送通知", "*/5 * * * *"),
    ("cleanup_uploads", "清理临时文件", "0 3 * * *"),
    ("backup_database", "数据库备份", "0 4 * * *"),
    ("risk_review", "风控复核", "*/30 * * * *"),
    ("analytics_rollup", "分析数据汇总", "0 * * * *"),
    ("health_probe", "服务健康巡检", "*/5 * * * *"),
)

# 旧版调度中心的代码仍可能被旧页面、脚本和已保存的配置调用。
# 别名不加入 TASK_CATALOG，避免同一任务被 APScheduler 重复注册。
TASK_ALIASES: dict[str, str] = {
    "polish": "refresh_listings",
    "fetch_orders": "sync_orders",
    "fetch_pending_orders": "sync_orders",
    "fetch_refund_orders": "sync_orders",
    "fetch_items": "sync_products",
    "login_renew": "refresh_cookies",
    "cookies_refresh": "refresh_cookies",
    "api_cookie_renew": "refresh_cookies",
    "token_renewal": "refresh_tokens",
    "rate": "auto_rate",
    "delivery_timeout": "shipment_timeout",
    "listing_monitor": "monitor_listings",
    "db_backup": "backup_database",
    "image_cleanup": "cleanup_uploads",
}

# 无法与当前执行器安全等价的旧任务必须明确拒绝，不能返回伪成功。
LEGACY_UNSUPPORTED_TASKS: frozenset[str] = frozenset()


def canonical_task_name(task_name: str) -> str:
    """返回调度器实际执行的任务代码。"""
    value = str(task_name or "").strip()
    return TASK_ALIASES.get(value, value)


LEGACY_TASK_NAMES: frozenset[str] = frozenset({*TASK_ALIASES, *LEGACY_UNSUPPORTED_TASKS})
