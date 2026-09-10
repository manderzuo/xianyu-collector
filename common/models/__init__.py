# -*- coding: utf-8 -*-
"""ORM 模型汇总。

导入全部模型以注册到 Base.metadata,
供 create_all / Alembic autogenerate 使用。
"""
from common.db.base import Base
from common.models.users import User
from common.models.registration_invites import RegistrationInvite
from common.models.accounts import Account
from common.models.messages import Message
from common.models.products import Product
from common.models.orders import Order
from common.models.card_delivery import CardDeliveryRecord
from common.models.risk_logs import RiskLog
from common.models.notifications import Notification
from common.models.account_cookies import AccountCookie
from common.models.rules import KeywordRule, DefaultReply
from common.models.catalog import Material, PublishLog, PublishAddress, CrawlItem
from common.models.monitor import MonitorTask, MonitorItem
from common.models.operations import ScheduledTask, Upload, Announcement, Advertisement, Feedback
from common.models.system import SystemSetting, ExternalConnection
from common.models.goofish_crawler import GoofishCrawlJob, GoofishCrawlResult
from common.models.feature_records import FeatureRecord
from common.models.account_contents import AccountContent, AccountSyncState
from common.models.chat import ChatMessageRecord
from common.models.notification_channels import NotificationChannel, MessageNotificationBinding
from common.models.risk_control_logs import RiskControlLog
from common.models.polish_logs import PolishLog
from common.models.extended import (
    QrSession, QrLoginSession, ProxyEndpoint, AiProvider, PublishJob, CapabilityCheck,
    AutoRateRule, RefundCase, DistributionItem, CompassMetric, Popup,
    SharedScanSession, SharedScanWorker, FaceVerification, PaymentRecord, RankingEntry,
)

__all__ = [
    "Base",
    "User",
    "RegistrationInvite",
    "Account",
    "Message",
    "Product",
    "Order", "CardDeliveryRecord",
    "RiskLog",
    "Notification",
    "AccountCookie", "KeywordRule", "DefaultReply", "Material", "PublishLog",
    "PublishAddress", "CrawlItem", "MonitorTask", "MonitorItem", "ScheduledTask",
    "Upload", "Announcement", "Advertisement", "Feedback", "SystemSetting", "ExternalConnection",
    "GoofishCrawlJob", "GoofishCrawlResult",
    "AccountContent", "AccountSyncState",
    "ChatMessageRecord", "NotificationChannel", "MessageNotificationBinding",
    "RiskControlLog", "PolishLog",
    "FeatureRecord",
    "QrSession", "QrLoginSession", "ProxyEndpoint", "AiProvider", "PublishJob", "CapabilityCheck",
    "AutoRateRule", "RefundCase", "DistributionItem", "CompassMetric", "Popup",
    "SharedScanSession", "SharedScanWorker", "FaceVerification", "PaymentRecord", "RankingEntry",
]
