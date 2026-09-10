# -*- coding: utf-8 -*-
"""双 AI 闲鱼客服对话包生成器。

这是一个独立的本地 GUI 工具，不依赖本项目的后端服务，也不会修改现有业务数据。
它使用两个 OpenAI 兼容接口分别模拟买家和卖家，然后用卖家接口把对话整理成
可审核的本地回复模板，并导出 JSON、JSONL 和关键词导入文件。API Key 会以当前
Windows 用户 DPAPI 加密方式保存在本机配置中，生成文件和日志不会包含 Key。

启动：
    python tools/dialogue_pack_generator.py

仅使用 Python 标准库。
"""

from __future__ import annotations

import json
import base64
import ctypes
import hashlib
from ctypes import wintypes
import os
import queue
import sys
import re
import threading
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import END, BOTH, DISABLED, NORMAL, BooleanVar, Canvas, Checkbutton, Entry, Frame, Label, StringVar, Text, Tk, filedialog, messagebox, ttk
from typing import Any, Callable


APP_TITLE = "闲鱼客服对话包生成器"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
CONFIG_FILE_NAME = "config.json"
MAX_SESSIONS_PER_SCENARIO = 1000
MAX_TURNS_PER_SESSION = 30
MAX_API_RETRIES_ON_TRUNCATION = 2
ALLOWED_VARIABLES = {
    "buyer_name",
    "item_title",
    "price",
    "original_price",
    "stock",
    "sellable_stock",
    "platform_stock",
    "card_stock",
    "delivery_method",
    "delivery_time",
    "order_id",
    "DELIVERY_CONTENT",
}
DEFAULT_SCENARIOS = """【售前咨询】打招呼与确认在线
【售前咨询】商品是什么、适合什么人
【售前咨询】商品内容、版本和规格
【售前咨询】价格、原价和优惠
【售前咨询】能否议价、批量购买优惠
【售前咨询】是否有货和剩余库存
【售前咨询】卡密格式、课件格式和文件大小
【售前咨询】支持的设备、系统和使用环境
【售前咨询】有效期、激活期限和使用次数
【售前咨询】是否需要账号、实名或绑定
【售前咨询】能否先试用、验货或查看样例
【售前咨询】是否支持指定版本或指定内容
【售中下单】拍下后如何操作
【售中下单】付款后什么时候自动发货
【售中下单】没有收到卡密或文件
【售中下单】重复拍下、多拍或拍错商品
【售中下单】买家催发货和订单状态
【售中下单】买家要求修改收货信息
【售后处理】卡密激活步骤
【售后处理】卡密无效、重复或已被使用
【售后处理】激活地址打不开或页面报错
【售后处理】课件打不开、缺文件或下载失败
【售后处理】版本不兼容、安装和使用问题
【售后处理】换卡、补发和人工核验
【售后处理】退款条件、退款流程和超时
【售后处理】商品与描述不一致
【售后处理】订单完成后的售后咨询
【沟通理解】口语化、缩写、错别字和少字消息
【沟通理解】连续追问和上下文指代
【沟通理解】买家只发送问号、表情或图片
【风险拦截】要求私下交易、外部转账或提供敏感信息
【风险拦截】索要后台账号、Cookie、验证码或卡密库存
【风险拦截】恶意退款、威胁投诉和异常索赔
【人工接管】低置信度问题和需要人工确认的事项"""

DEFAULT_FACTS = """【商品基本信息】
商品类型：数字商品 / CDKey / 课件（请按实际情况修改）
商品名称：请在上方“商品名称”填写
商品版本或规格：待填写；未填写时不得自行猜测
商品包含内容：待填写；不能把未列出的内容说成已包含

【价格与库存】
售价：待填写；运行时优先读取商品实时售价
原价：待填写；没有原价时不要主动提及
库存来源：CDKey 使用本地未使用卡密数量；课件使用本地可交付资源数量
库存变量：优先使用 {sellable_stock}，不要把平台展示库存当作卡密真实库存
库存为 0 时：不要承诺可发货，建议回复“当前库存不足，请联系客服确认”
是否公开具体库存：待填写；未填写时只回复“目前有货/库存以系统为准”

【交付规则】
交付方式：买家付款后自动发送 / 人工发送（请按实际情况修改）
发货时间：待填写；未填写不得承诺具体分钟数
交付变量：{DELIVERY_CONTENT}
一笔订单发放数量：待填写；未填写不得承诺多份
自动发货失败：先核验订单状态和库存，再转人工处理

【激活与使用】
激活地址：待填写；未填写不得编造链接
激活步骤：待填写；建议按“打开地址 → 登录/输入卡密 → 确认激活”补充真实步骤
有效期：待填写；不能承诺永久有效
使用次数：待填写；不能承诺无限次使用
支持设备或系统：待填写；不确定时必须先核实
账号绑定规则：待填写；不能承诺可解绑、可转让或多设备使用

【售后与退款】
卡密无效处理：保留订单号和错误截图，核验后按实际情况换卡或人工处理
重复卡密处理：禁止直接再次发送库存，先查询发货记录
文件缺失或打不开：先核对订单和文件版本，再补发或转人工
退款规则：待填写；未填写时不要承诺一定可以退款
换卡规则：待填写；只有核验通过后才允许换卡
人工客服时间：待填写；未填写时不要承诺具体在线时间

【安全与合规】
不索要买家的密码、短信验证码、支付密码、Cookie 或身份证信息
不引导买家绕过平台交易或私下转账
不承诺平台无法确认的结果，不编造订单、库存、价格或激活状态
遇到纠纷、投诉、异常退款或事实不足时，标记 needs_human=true 并转人工

【模板变量】
可使用：{buyer_name} {item_title} {price} {original_price} {stock} {sellable_stock}
{platform_stock} {card_stock} {delivery_method} {delivery_time} {order_id} {DELIVERY_CONTENT}
带“待填写”的字段视为未知，回复中不得原样输出“待填写”。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _local_config_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.cwd())
    return Path(root) / "XianyuDialoguePackGenerator" / CONFIG_FILE_NAME


def _protect_secret(value: str) -> str:
    """使用 Windows DPAPI 按当前用户加密 API Key。"""
    if not value:
        return ""
    if os.name != "nt":
        return "plain:" + value
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    raw = value.encode("utf-8")
    buffer = (ctypes.c_char * len(raw)).from_buffer_copy(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = _DataBlob()
    if not crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise OSError("Windows DPAPI 加密失败")
    try:
        encrypted = ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
    return "dpapi:" + base64.b64encode(encrypted).decode("ascii")


def _unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith("plain:"):
        return value[6:]
    if not value.startswith("dpapi:") or os.name != "nt":
        return ""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    raw = base64.b64decode(value[6:])
    buffer = (ctypes.c_char * len(raw)).from_buffer_copy(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = _DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        return ""
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(target.pbData)


class ApiError(RuntimeError):
    """OpenAI 兼容接口调用失败。"""

    def __init__(self, message: str, *, truncated: bool = False):
        super().__init__(message)
        self.truncated = truncated


class OutputRunLock:
    """同一输出目录只允许一个生成任务，避免多个 GUI 覆盖同一个断点文件。"""

    def __init__(self, output_dir: Path):
        self.path = output_dir / "dialogue_pack.run.lock"
        self.handle: int | None = None

    def __enter__(self) -> "OutputRunLock":
        payload = f"pid={os.getpid()}\nstarted_at={now_iso()}\n"
        for attempt in range(2):
            try:
                self.handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.handle, payload.encode("utf-8"))
                return self
            except FileExistsError:
                if attempt:
                    raise ApiError(f"输出目录已有生成任务在运行：{self.path}")
                try:
                    content = self.path.read_text(encoding="utf-8")
                    pid_match = re.search(r"pid=(\d+)", content)
                    old_pid = int(pid_match.group(1)) if pid_match else 0
                    if old_pid and old_pid != os.getpid():
                        os.kill(old_pid, 0)
                    raise ApiError(f"输出目录已有生成任务在运行（PID {old_pid or '未知'}）")
                except ProcessLookupError:
                    # 进程已退出但锁文件遗留，可以安全清掉后重试一次。
                    self.path.unlink(missing_ok=True)
                except PermissionError:
                    raise ApiError(f"输出目录已有生成任务在运行：{self.path}")
        raise ApiError(f"无法取得输出目录锁：{self.path}")

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.handle is not None:
            os.close(self.handle)
            self.handle = None
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    api_key: str
    model: str
    proxy_url: str = ""


@dataclass
class JobConfig:
    buyer: ApiConfig
    seller: ApiConfig
    product_id: str
    product_title: str
    facts: str
    scenarios: list[str]
    sessions_per_scenario: int
    max_turns: int
    output_dir: Path
    use_organizer: bool
    buyer_max_output_tokens: int = 80
    seller_max_output_tokens: int = 120
    organizer_max_output_tokens: int = 600
    resume_enabled: bool = True
    resume_file: Path | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clean_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("API 地址不能为空")
    if not re.match(r"^https?://", value, flags=re.I):
        raise ValueError("API 地址必须以 http:// 或 https:// 开头")
    return value


def response_is_truncated(payload: Any) -> bool:
    """识别 Responses/Chat Completions 因输出上限而结束的响应。"""
    if not isinstance(payload, dict):
        return False
    incomplete = payload.get("incomplete_details")
    reason = str(incomplete.get("reason") if isinstance(incomplete, dict) else "").lower()
    status = str(payload.get("status") or "").lower()
    if reason in {"max_output_tokens", "length", "token_limit", "max_tokens"}:
        return True
    if status == "incomplete" and (not incomplete or reason in {"", "max_output_tokens", "length"}):
        return True
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and str(choice.get("finish_reason") or "").lower() in {"length", "max_tokens"}:
                return True
    error = payload.get("error")
    error_text = json.dumps(error, ensure_ascii=False).lower() if error else ""
    return any(marker in error_text for marker in ("max_output_tokens", "max_tokens", "token limit", "length"))


def extract_content(payload: Any) -> str:
    """兼容 Chat Completions 和 OpenAI Responses 的字符串或多段 content。"""
    if isinstance(payload, dict):
        error = payload.get("error")
        status = str(payload.get("status") or "")
        incomplete = payload.get("incomplete_details")
        output_value = payload.get("output")
        # Responses 推理模型可能返回 status=incomplete、output=[]，但 error 为空。
        # 这时必须把服务端给出的终止原因展示出来，不能再误报“没有 choices”。
        if error or status in {"failed", "incomplete", "cancelled"} or ("output" in payload and not output_value and "choices" not in payload):
            if isinstance(error, dict):
                error_text = str(error.get("message") or error.get("type") or error)
            else:
                error_text = str(error or "未提供 error.message")
            suffix = f"；状态：{status}" if status else ""
            if incomplete:
                suffix += f"；未完成详情：{str(incomplete)[:300]}"
            reasoning = payload.get("reasoning")
            if reasoning:
                suffix += f"；推理摘要：{str(reasoning)[:300]}"
            raise ApiError(f"Responses 未生成可发送文本：{error_text[:800]}{suffix}", truncated=response_is_truncated(payload))
        # 一些兼容网关会把标准 Responses 包装在 data/result/response 中。
        for wrapper in ("data", "result", "response"):
            nested = payload.get(wrapper)
            if isinstance(nested, (dict, list)):
                try:
                    value = extract_content(nested)
                    if value:
                        return value
                except ApiError:
                    pass
        # Chat Completions 可能同时返回部分 content 和 finish_reason=length。
        # 部分网关会把它伪装成成功响应，必须丢弃这次半截文本并自动重试。
        if response_is_truncated(payload):
            raise ApiError("接口输出达到 Token 上限，已请求扩大上限重试", truncated=True)
    if isinstance(payload, dict) and isinstance(payload.get("output_text"), str):
        value = payload["output_text"].strip()
        if value:
            return value
    if isinstance(payload, dict) and isinstance(payload.get("output"), (list, dict)):
        output_items = payload["output"] if isinstance(payload["output"], list) else [payload["output"]]
        parts: list[str] = []
        for output_item in output_items:
            if not isinstance(output_item, dict):
                continue
            content = output_item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            elif isinstance(output_item.get("text"), str):
                parts.append(output_item["text"])
        value = "".join(parts).strip()
        if value:
            return value
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        if isinstance(payload, dict):
            fields = ", ".join(str(key) for key in list(payload.keys())[:20]) or "空对象"
            raise ApiError(f"接口返回没有可识别的文本字段，返回字段：{fields}")
        raise ApiError("接口返回不是可识别的对象")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
        raise ApiError("接口返回空回复", truncated=response_is_truncated(payload))


class OpenAICompatibleClient:
    def __init__(self, config: ApiConfig, timeout: int = 90, max_output_tokens: int | None = None):
        self.config = config
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        proxy_url = str(config.proxy_url or "").strip()
        if proxy_url and not re.match(r"^https?://", proxy_url, flags=re.I):
            proxy_url = "http://" + proxy_url
        if proxy_url and not re.match(r"^https?://[^/]+", proxy_url, flags=re.I):
            raise ApiError("代理地址格式错误，请填写 http://127.0.0.1:10809")
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}) if proxy_url else urllib.request.ProxyHandler()
        )

    def chat(self, messages: list[dict[str, str]], temperature: float | None = 0.7) -> str:
        base_url = clean_base_url(self.config.base_url)
        model = self.config.model.strip()
        # OpenCode Zen 的 Muse Spark 系列使用 Responses 接口；其他常见模型通常
        # 使用 Chat Completions。允许用户直接填完整端点，也自动识别 Muse 1.2/1.3。
        if base_url.endswith("/responses") or model.startswith("muse-spark-"):
            url = base_url if base_url.endswith("/responses") else base_url + "/responses"
            body = {"model": model, "input": messages}
            # Muse Spark 默认可能使用 high 推理强度，短客服回复会在达到
            # max_output_tokens 前耗尽预算而没有 output。生成模板时使用 minimal，
            # 保留足够预算给最终可发送文本。
            if model.startswith("muse-spark-"):
                body["reasoning"] = {"effort": "minimal"}
            output_limit_key = "max_output_tokens"
        else:
            url = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
            body = {"model": model, "messages": messages}
            output_limit_key = "max_tokens"
        if temperature is not None:
            body["temperature"] = temperature
        output_limit = int(self.max_output_tokens) if self.max_output_tokens is not None else None
        for attempt in range(MAX_API_RETRIES_ON_TRUNCATION + 1):
            request_body = dict(body)
            if output_limit is not None:
                # Responses 使用 max_output_tokens，Chat Completions 使用 max_tokens。
                request_body[output_limit_key] = output_limit
            request = urllib.request.Request(
                url,
                data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    # 部分 API 网关的 Cloudflare Browser Integrity Check 会拦截
                    # Python-urllib 默认 UA；这里声明普通浏览器请求特征。
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
                    "Authorization": f"Bearer {self.config.api_key.strip()}",
                },
                method="POST",
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
                if exc.code == 403 and "1010" in detail:
                    raise ApiError(
                        "HTTP 403 / Cloudflare 1010：服务商按客户端特征拒绝了请求，通常不是 API Key 错误。"
                        "请确认填写的是 API 根地址（例如 https://域名/v1），不是网页控制台地址；"
                        "若仍失败，需要 API 服务商放行当前来源或提供专用 API 域名。"
                    ) from exc
                if exc.code == 401:
                    raise ApiError("HTTP 401：API Key 无效、已过期，或当前 Key 没有该模型权限") from exc
                if exc.code == 404:
                    raise ApiError("HTTP 404：API 地址或路径不存在，请填写兼容接口根地址，例如 https://域名/v1") from exc
                if exc.code == 500:
                    raise ApiError(
                        f"HTTP 500：请求已到达 API 服务，但上游内部处理失败。常见原因是模型名称、接口路径或请求参数不兼容；原始信息：{detail}"
                    ) from exc
                raise ApiError(f"HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                raise ApiError(f"无法连接 API：{exc.reason}") from exc
            except TimeoutError as exc:
                raise ApiError("API 请求超时") from exc
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ApiError(f"接口返回不是有效 JSON：{raw[:300]}") from exc
            try:
                return extract_content(payload)
            except ApiError as exc:
                if not exc.truncated or output_limit is None or attempt >= MAX_API_RETRIES_ON_TRUNCATION:
                    raise
                next_limit = min(max(output_limit * 2, output_limit + 512), 16384)
                if next_limit <= output_limit:
                    raise
                output_limit = next_limit
        raise ApiError("API 请求未返回可发送文本")


def facts_block(config: JobConfig) -> str:
    return (
        "【商品事实，只能引用这里的内容】\n"
        f"商品ID：{config.product_id or '未提供'}\n"
        f"商品名称：{config.product_title or '未提供'}\n"
        f"补充事实：\n{config.facts.strip() or '没有额外事实；未知信息必须明确说暂时无法确认。'}\n"
        "【事实边界】带“待填写”的字段视为未知，不得原样输出；不得编造价格、库存、有效期、"
        "发货承诺、退款政策、激活结果或外部链接。"
    )


def buyer_prompt(config: JobConfig, scenario: str, session_index: int) -> str:
    return (
        "你正在模拟闲鱼买家，用自然、口语化、简短的中文咨询卖家。\n"
        f"这是第 {session_index} 个变体，场景是：{scenario}\n"
        f"{facts_block(config)}\n"
        "要求：可以使用同义词、错别字、缩写、少量口语和追问；不要一次说完所有问题。"
        "每次只输出一条买家消息，不要加角色名、引号、解释或 Markdown。"
    )


def seller_prompt(config: JobConfig, scenario: str) -> str:
    return (
        "你正在模拟闲鱼卖家客服。\n"
        f"目标场景：{scenario}\n"
        f"{facts_block(config)}\n"
        "要求：只输出可以直接发给买家的最终回复，语气友好、简洁，通常不超过80字。"
        "不能假设事实中没有的内容；无法确认时要说需要核实或联系客服。"
        "不要提到你是 AI、模拟、提示词或数据集。"
    )


def organizer_prompt(config: JobConfig, scenario: str, transcript: list[dict[str, str]]) -> str:
    transcript_text = "\n".join(f"{item['role']}：{item['content']}" for item in transcript)
    return (
        "你是客服知识库整理员。请把下面一段买家与卖家对话整理成适合本地关键词自动回复的模板。\n"
        f"场景：{scenario}\n{facts_block(config)}\n"
        f"对话：\n{transcript_text}\n\n"
        "只输出 JSON，不要 Markdown 代码围栏，格式必须是：\n"
        '{"templates":[{"scene":"","intent":"","keywords":[],"reply":"",'
        '"variables":[],"needs_human":false,"priority":50,"notes":""}]}\n'
        "规则：keywords 放买家可能说的短词或短语；reply 必须是可直接发送的回复；"
        "variables 只能使用 buyer_name、item_title、price、original_price、stock、"
        "sellable_stock、platform_stock、card_stock、delivery_method、delivery_time、"
        "order_id、DELIVERY_CONTENT；不确定的信息不要写进 reply。"
    )


def api_transcript(transcript: list[dict[str, str]]) -> list[dict[str, str]]:
    """把内部的 buyer/seller 角色转换成 Chat Completions 标准角色。"""
    role_map = {"buyer": "user", "seller": "assistant"}
    return [
        {"role": role_map.get(str(item.get("role")), "user"), "content": str(item.get("content") or "")}
        for item in transcript
        if str(item.get("content") or "").strip()
    ]


def parse_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def normalize_templates(raw: dict[str, Any] | None, scenario: str) -> list[dict[str, Any]]:
    if not raw or not isinstance(raw.get("templates"), list):
        return []
    result: list[dict[str, Any]] = []
    for candidate in raw["templates"]:
        if not isinstance(candidate, dict):
            continue
        reply = re.sub(r"\s+", " ", str(candidate.get("reply") or "").strip())
        if not reply:
            continue
        keywords: list[str] = []
        for item in candidate.get("keywords") or []:
            word = re.sub(r"\s+", "", str(item or "").strip())
            if 1 < len(word) <= 30 and word not in keywords:
                keywords.append(word)
        if not keywords:
            keywords = [scenario[:30]]
        variables = [str(item) for item in candidate.get("variables") or [] if str(item) in ALLOWED_VARIABLES]
        try:
            priority = max(1, min(100, int(candidate.get("priority", 50))))
        except (TypeError, ValueError):
            priority = 50
        result.append({
            "scene": str(candidate.get("scene") or scenario).strip()[:100],
            "intent": str(candidate.get("intent") or scenario).strip()[:100],
            "keywords": keywords[:20],
            # 不在本地二次截断；API 已通过“达到上限自动重试”处理真正的截断。
            "reply": reply,
            "variables": list(dict.fromkeys(variables)),
            "needs_human": bool(candidate.get("needs_human", False)),
            "priority": priority,
            "notes": str(candidate.get("notes") or "").strip()[:500],
            "source": "synthetic",
        })
    return result


def fallback_template(scenario: str, transcript: list[dict[str, str]]) -> dict[str, Any] | None:
    seller_messages = [item["content"] for item in transcript if item.get("role") == "seller"]
    buyer_messages = [item["content"] for item in transcript if item.get("role") == "buyer"]
    if not seller_messages:
        return None
    keywords = [scenario]
    if buyer_messages:
        first = buyer_messages[0]
        for match in re.findall(r"[\u4e00-\u9fff]{2,8}", first):
            if match not in keywords:
                keywords.append(match)
    return {
        "scene": scenario,
        "intent": scenario,
        "keywords": keywords[:12],
        "reply": seller_messages[-1],
        "variables": [],
        "needs_human": False,
        "priority": 50,
        "notes": "AI 整理失败，保留最后一条卖家回复；请人工审核后启用。",
        "source": "synthetic_fallback",
    }


def deduplicate_templates(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for template in templates:
        key = (template.get("reply", ""), template.get("intent", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(template)
    return result


def keywords_import(templates: list[dict[str, Any]], product_id: str) -> list[dict[str, Any]]:
    return [
        {
            "keyword": "\n".join(template["keywords"]),
            "reply": template["reply"],
            "item_id": product_id,
            "type": "text",
            "status": "active",
            "priority": template["priority"],
            "scene": template["scene"],
            "needs_human": template["needs_human"],
            "variables": template["variables"],
            "source": template["source"],
        }
        for template in templates
        if template.get("keywords") and template.get("reply")
    ]


def session_key(scenario: str, session_index: int) -> str:
    return f"{scenario}\x1f{session_index}"


def int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def input_fingerprint(config: JobConfig, scenario: str) -> str:
    """只对会影响生成内容的输入做指纹，不包含 API Key。"""
    payload = {
        "format": "xianyu-dialogue-generator-input-v2",
        "product_id": config.product_id,
        "product_title": config.product_title,
        "facts": config.facts,
        "scenario": scenario,
        "max_turns": config.max_turns,
        "use_organizer": config.use_organizer,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def log_preview(value: str, limit: int = 36) -> str:
    """日志只显示短预览，并明确标出完整文本长度；导出文件不使用此函数。"""
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}…（预览 {limit}/{len(compact)} 字）"


class Generator:
    def __init__(self, config: JobConfig, log: Callable[[str], None], stop_event: threading.Event):
        self.config = config
        self.log = log
        self.stop_event = stop_event
        self.buyer = OpenAICompatibleClient(config.buyer, max_output_tokens=config.buyer_max_output_tokens)
        self.seller = OpenAICompatibleClient(config.seller, max_output_tokens=config.seller_max_output_tokens)
        self.organizer = OpenAICompatibleClient(config.seller, max_output_tokens=config.organizer_max_output_tokens)

    def simulate(
        self,
        scenario: str,
        session_index: int,
        initial_transcript: list[dict[str, str]] | None = None,
        checkpoint: Callable[[list[dict[str, str]]], None] | None = None,
    ) -> list[dict[str, str]]:
        transcript: list[dict[str, str]] = [
            {"role": str(item.get("role")), "content": str(item.get("content") or "")}
            for item in (initial_transcript or [])
            if str(item.get("content") or "").strip() and str(item.get("role")) in {"buyer", "seller"}
        ]
        buyer_system = buyer_prompt(self.config, scenario, session_index)
        seller_system = seller_prompt(self.config, scenario)
        next_buyer_instruction = (
            "请先提出本场景下最自然的第一个问题。" if not transcript
            else "请根据卖家上一条回复自然追问一个相关问题；如果问题已经解决，回复“谢谢，先这样”并结束。"
        )
        turn = len(transcript) // 2
        while turn < self.config.max_turns:
            if self.stop_event.is_set():
                break
            buyer_text = ""
            seller_text = ""
            # 如果程序在买家消息之后中断，恢复时从卖家回复开始，避免重复消耗一次买家调用。
            if len(transcript) % 2 == 0:
                buyer_messages = [
                    {"role": "system", "content": buyer_system},
                    *api_transcript(transcript[-12:]),
                    {"role": "user", "content": next_buyer_instruction},
                ]
                buyer_text = self.buyer.chat(buyer_messages, temperature=0.9)
                buyer_text = re.sub(r"^(买家|客户)[:：]\s*", "", buyer_text).strip()
                if not buyer_text:
                    raise ApiError("买家 AI 返回空消息")
                # 不做 [:500] 之类的硬截断，保留模型返回的完整消息。
                transcript.append({"role": "buyer", "content": buyer_text})
                if checkpoint:
                    checkpoint(transcript)
            if self.stop_event.is_set():
                break
            seller_messages = [
                {"role": "system", "content": seller_system},
                *api_transcript(transcript[-12:]),
                {"role": "user", "content": "请回复买家上一条消息，只输出最终回复。"},
            ]
            seller_text = self.seller.chat(seller_messages, temperature=0.45)
            seller_text = re.sub(r"^(卖家|客服)[:：]\s*", "", seller_text).strip()
            if not seller_text:
                raise ApiError("卖家 AI 返回空回复")
            transcript.append({"role": "seller", "content": seller_text})
            if checkpoint:
                checkpoint(transcript)
            self.log(
                f"    回合 {turn + 1}: 买家 {log_preview(buyer_text or transcript[-2]['content'])}"
                f" / 卖家 {log_preview(seller_text)}"
            )
            next_buyer_instruction = (
                "请根据卖家上一条回复自然追问一个相关问题；如果问题已经解决，回复“谢谢，先这样”并结束。"
            )
            if any(marker in transcript[-2]["content"] for marker in ("谢谢", "先这样", "明白了", "好的不用了")):
                break
            turn += 1
        return transcript

    def run(self) -> dict[str, Any]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        with OutputRunLock(self.config.output_dir):
            return self._run_locked()

    def _run_locked(self) -> dict[str, Any]:
        total = len(self.config.scenarios) * self.config.sessions_per_scenario
        sessions: list[dict[str, Any]] = []
        templates: list[dict[str, Any]] = []
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        source_package: dict[str, Any] | None = None
        source_path = self.config.resume_file or (self.config.output_dir / "dialogue_pack.partial.json")
        if self.config.resume_enabled and source_path.exists():
            try:
                loaded = json.loads(source_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("sessions"), list):
                    source_package = loaded
                    self.log(f"发现断点文件：{source_path}")
            except Exception as exc:
                self.log(f"断点文件读取失败，将从头生成：{str(exc)[:300]}")

        # 旧版本没有 input_fingerprint。只在商品事实、回合设置和整理设置都相同的情况下兼容复用，
        # 一旦用户修改商品事实，就自动淘汰旧会话，避免把旧商品内容混进新包。
        source_generation = source_package.get("generation", {}) if source_package else {}
        source_product = source_package.get("product", {}) if source_package else {}
        source_scenarios = source_generation.get("scenarios")
        scenarios_compatible = not isinstance(source_scenarios, list) or source_scenarios == self.config.scenarios
        legacy_compatible = bool(source_package) and (
            source_product.get("id", "") == self.config.product_id
            and source_product.get("title", "") == self.config.product_title
            and source_product.get("facts", "") == self.config.facts
            and int_or_default(source_generation.get("max_turns"), self.config.max_turns) == self.config.max_turns
            and bool(source_generation.get("uses_organizer_ai", self.config.use_organizer)) == self.config.use_organizer
            and scenarios_compatible
        )
        source_by_key: dict[str, dict[str, Any]] = {}
        if source_package:
            for item in source_package.get("sessions") or []:
                if not isinstance(item, dict):
                    continue
                key = session_key(str(item.get("scenario") or ""), int_or_default(item.get("session_index"), 0))
                if key.split("\x1f", 1)[0] in self.config.scenarios:
                    source_by_key[key] = item

        reusable_keys: set[str] = set()
        for scenario in self.config.scenarios:
            fingerprint = input_fingerprint(self.config, scenario)
            for session_index in range(1, self.config.sessions_per_scenario + 1):
                key = session_key(scenario, session_index)
                previous = source_by_key.get(key)
                if previous and (previous.get("input_fingerprint") == fingerprint or (not previous.get("input_fingerprint") and legacy_compatible)):
                    reusable_keys.add(key)
        if source_package and reusable_keys:
            self.log(f"可复用断点会话 {len(reusable_keys)} 个；已完成会话将跳过，未完成/失败会话将继续。")

        if source_package:
            for item in source_package.get("templates") or []:
                if not isinstance(item, dict):
                    continue
                template_key = str(item.get("source_session_key") or "")
                if (template_key and template_key in reusable_keys) or (not template_key and legacy_compatible):
                    templates.append(item)

        def upsert(record: dict[str, Any]) -> None:
            key = session_key(str(record.get("scenario") or ""), int_or_default(record.get("session_index"), 0))
            for index, current in enumerate(sessions):
                if session_key(str(current.get("scenario") or ""), int_or_default(current.get("session_index"), 0)) == key:
                    sessions[index] = record
                    return
            sessions.append(record)

        def completed_count() -> int:
            return sum(1 for item in sessions if item.get("status") == "completed")

        for scenario in self.config.scenarios:
            for session_index in range(1, self.config.sessions_per_scenario + 1):
                if self.stop_event.is_set():
                    break
                key = session_key(scenario, session_index)
                fingerprint = input_fingerprint(self.config, scenario)
                previous = source_by_key.get(key) if key in reusable_keys else None
                if previous and previous.get("status") == "completed":
                    upsert(previous)
                    self.log(f"跳过已完成会话：{scenario}（变体 {session_index}/{self.config.sessions_per_scenario}）")
                    continue
                transcript: list[dict[str, str]] = list(previous.get("turns") or []) if previous else []
                phase = str(previous.get("phase") or "simulate") if previous else "simulate"
                record: dict[str, Any] = {
                    "scenario": scenario,
                    "session_index": session_index,
                    "turns": transcript,
                    "status": "in_progress",
                    "phase": phase,
                    "input_fingerprint": fingerprint,
                    "created_at": str(previous.get("created_at") or now_iso()) if previous else now_iso(),
                    "updated_at": now_iso(),
                }
                upsert(record)
                self.save(sessions, templates, total, completed_count())
                self.log(f"开始场景：{scenario}（变体 {session_index}/{self.config.sessions_per_scenario}）")
                try:
                    if phase != "organize":
                        phase = "simulate"
                        record["phase"] = phase

                        def checkpoint(current: list[dict[str, str]]) -> None:
                            record["turns"] = list(current)
                            record["updated_at"] = now_iso()
                            self.save(sessions, templates, total, completed_count())

                        transcript = self.simulate(scenario, session_index, transcript, checkpoint)
                    if self.stop_event.is_set():
                        record["turns"] = transcript
                        record["status"] = "in_progress"
                        record["phase"] = phase
                        self.save(sessions, templates, total, completed_count())
                        break
                    organized: list[dict[str, Any]] = []
                    if self.config.use_organizer and not self.stop_event.is_set():
                        phase = "organize"
                        record["phase"] = phase
                        record["turns"] = transcript
                        self.save(sessions, templates, total, completed_count())
                        raw = self.organizer.chat(
                            [{"role": "system", "content": organizer_prompt(self.config, scenario, transcript)}],
                            temperature=0.2,
                        )
                        organized = normalize_templates(parse_json_object(raw), scenario)
                    organized = organized or ([fallback_template(scenario, transcript)] if fallback_template(scenario, transcript) else [])
                    for template in organized:
                        template["source_session_key"] = key
                    templates.extend(organized)
                    record.update({
                        "turns": transcript,
                        "status": "completed",
                        "phase": "done",
                        "updated_at": now_iso(),
                    })
                    upsert(record)
                    self.save(sessions, templates, total, completed_count())
                    self.log(f"完成 {completed_count()}/{total}，新增模板 {len(organized)} 条")
                except Exception as exc:  # 单个场景失败不影响其余场景
                    record.update({
                        "turns": transcript,
                        "status": "in_progress" if self.stop_event.is_set() else "failed",
                        "phase": phase,
                        "error": str(exc)[:1000],
                        "updated_at": now_iso(),
                    })
                    upsert(record)
                    self.save(sessions, templates, total, completed_count())
                    self.log(f"本场景{('已暂停' if self.stop_event.is_set() else '失败')}，已保留断点：{str(exc)[:300]}")
            if self.stop_event.is_set():
                break
        final = self.save(sessions, templates, total, completed_count(), "stopped" if self.stop_event.is_set() else "completed")
        self.write_files(final)
        return final

    def save(self, sessions: list[dict[str, Any]], templates: list[dict[str, Any]], total: int, completed: int, status: str = "running") -> dict[str, Any]:
        package = {
            "format": "xianyu-local-dialogue-pack/v1",
            "status": status,
            "created_at": now_iso(),
            "product": {
                "id": self.config.product_id,
                "title": self.config.product_title,
                "facts": self.config.facts,
            },
            "generation": {
                "total_sessions": total,
                "completed_sessions": completed,
                "scenario_count": len(self.config.scenarios),
                "scenarios": self.config.scenarios,
                "sessions_per_scenario": self.config.sessions_per_scenario,
                "max_turns": self.config.max_turns,
                "uses_organizer_ai": self.config.use_organizer,
                "max_output_tokens": {
                    "buyer": self.config.buyer_max_output_tokens,
                    "seller": self.config.seller_max_output_tokens,
                "organizer": self.config.organizer_max_output_tokens,
                },
                "input_fingerprint_version": "per-session-v2",
            },
            "templates": deduplicate_templates(templates),
            "sessions": sessions,
            "review_required": True,
            "warning": "这是 AI 合成内容，启用前必须依据真实商品事实人工审核。",
        }
        path = self.config.output_dir / "dialogue_pack.partial.json"
        # 原子替换，避免程序被强制关闭时 partial JSON 只写了一半。
        temporary = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.config.output_dir, delete=False, suffix=".tmp")
        temporary_path = Path(temporary.name)
        try:
            json.dump(package, temporary, ensure_ascii=False, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            os.replace(temporary_path, path)
        finally:
            if not temporary.closed:
                temporary.close()
            if temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
        return package

    def write_files(self, package: dict[str, Any]) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = self.config.output_dir
        final_json = output / f"dialogue_pack_{stamp}.json"
        final_json.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        templates = package.get("templates") or []
        with (output / f"templates_{stamp}.jsonl").open("w", encoding="utf-8") as handle:
            for item in templates:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        (output / f"keywords_import_{stamp}.json").write_text(
            json.dumps(keywords_import(templates, self.config.product_id), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.log(f"已导出：{final_json}")


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1060x820")
        self.root.minsize(900, 680)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.shared_api = BooleanVar(value=True)
        self.organizer_enabled = BooleanVar(value=True)
        self._build_ui()
        self._load_saved_config()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(150, self._poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=BOTH, expand=True)
        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text=APP_TITLE, font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        ttk.Label(top, text="本地生成 · 不修改现有系统", foreground="#667085").pack(side="left", padx=12)

        canvas = Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=content, anchor="nw", width=1010)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill=BOTH, expand=True, pady=(10, 0))
        scrollbar.pack(side="right", fill="y", pady=(10, 0))

        api_frame = ttk.LabelFrame(content, text="1. API 配置（Key 使用 Windows DPAPI 加密保存到本机）", padding=10)
        api_frame.pack(fill="x", pady=(0, 10))
        self.buyer_base = self._field(api_frame, "买家 API 地址", DEFAULT_BASE_URL, 0, 0)
        self.buyer_key = self._field(api_frame, "买家 API Key", "", 0, 2, secret=True)
        self.buyer_model = self._field(api_frame, "买家模型", "gpt-4o-mini", 1, 0)
        self.seller_base = self._field(api_frame, "卖家/整理 API 地址", DEFAULT_BASE_URL, 1, 2)
        self.seller_key = self._field(api_frame, "卖家 API Key", "", 2, 0, secret=True)
        self.seller_model = self._field(api_frame, "卖家模型", "gpt-4o-mini", 2, 2)
        Checkbutton(api_frame, text="买家和卖家共用同一套 API（卖家配置覆盖买家配置）", variable=self.shared_api, command=self._toggle_shared).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(api_frame, text="测试 API", command=self.test_api).grid(row=3, column=3, sticky="e", pady=(6, 0))
        self.proxy_url = self._field(api_frame, "HTTP 代理（可空）", "http://127.0.0.1:10809", 4, 0)
        ttk.Label(api_frame, text="仅支持 HTTP/HTTPS 代理；10809 需确认是 HTTP 或混合端口").grid(row=4, column=2, columnspan=2, sticky="w", padx=(6, 0), pady=4)
        ttk.Button(api_frame, text="保存配置", command=self.save_config).grid(row=5, column=2, sticky="e", pady=(6, 0))
        ttk.Button(api_frame, text="清除已保存配置", command=self.clear_saved_config).grid(row=5, column=3, sticky="e", pady=(6, 0))
        for column in (1, 3):
            api_frame.columnconfigure(column, weight=1)
        self._toggle_shared()

        product_frame = ttk.LabelFrame(content, text="2. 商品事实（AI 只能使用这里的内容）", padding=10)
        product_frame.pack(fill="x", pady=(0, 10))
        self.product_id = self._field(product_frame, "商品 ID（可空）", "", 0, 0)
        self.product_title = self._field(product_frame, "商品名称", "自动发货商品", 0, 2)
        ttk.Label(product_frame, text="事实/规则（价格、库存、有效期、发货、退款等）").grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 3))
        self.facts = Text(product_frame, height=7, wrap="word")
        self.facts.grid(row=2, column=0, columnspan=4, sticky="ew")
        self.facts.insert("1.0", DEFAULT_FACTS)
        for column in (1, 3):
            product_frame.columnconfigure(column, weight=1)

        run_frame = ttk.LabelFrame(content, text="3. 生成任务", padding=10)
        run_frame.pack(fill="x", pady=(0, 10))
        self.sessions = self._field(run_frame, "每个场景变体数", "3", 0, 0)
        self.turns = self._field(run_frame, "每场最大回合数", "4", 0, 2)
        self.output_dir = self._field(run_frame, "输出目录", str(Path.cwd() / "dialogue_packs"), 1, 0)
        ttk.Button(run_frame, text="选择目录", command=self.choose_output).grid(row=1, column=3, sticky="e")
        self.resume_enabled = BooleanVar(value=True)
        Checkbutton(run_frame, text="启用断点续传（自动读取输出目录下的 partial 文件）", variable=self.resume_enabled).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(run_frame, text="指定已有包（可空）").grid(row=2, column=2, sticky="w", padx=(6, 0), pady=4)
        self.resume_file = ttk.Entry(run_frame)
        self.resume_file.grid(row=2, column=3, sticky="ew", pady=4)
        ttk.Button(run_frame, text="选择文件", command=self.choose_resume_file).grid(row=3, column=3, sticky="e", pady=(0, 4))
        self.buyer_output_tokens = self._field(run_frame, "买家单次输出 Token", "1024", 4, 0)
        self.seller_output_tokens = self._field(run_frame, "卖家单次回复 Token", "1536", 4, 2)
        self.organizer_output_tokens = self._field(run_frame, "整理单次输出 Token", "2048", 5, 0)
        ttk.Label(run_frame, text="达到上限会自动扩大并重试；Muse 推荐 1024/1536/2048").grid(row=5, column=2, columnspan=2, sticky="w", padx=(6, 0), pady=4)
        ttk.Label(run_frame, text="场景（一行一个）").grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 3))
        self.scenarios = Text(run_frame, height=8, wrap="word")
        self.scenarios.grid(row=7, column=0, columnspan=4, sticky="ew")
        self.scenarios.insert("1.0", DEFAULT_SCENARIOS)
        Checkbutton(run_frame, text="生成后调用卖家 API 整理模板（建议开启）", variable=self.organizer_enabled).grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.start_button = ttk.Button(run_frame, text="开始后台生成", command=self.start)
        self.start_button.grid(row=8, column=3, sticky="e", pady=(6, 0))
        self.stop_button = ttk.Button(run_frame, text="停止并保留已完成结果", command=self.stop, state=DISABLED)
        self.stop_button.grid(row=8, column=2, sticky="e", padx=8, pady=(6, 0))
        for column in (1, 3):
            run_frame.columnconfigure(column, weight=1)

        log_frame = ttk.LabelFrame(content, text="运行日志", padding=8)
        log_frame.pack(fill=BOTH, expand=True)
        self.progress = ttk.Progressbar(log_frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 6))
        self.log_text = Text(log_frame, height=14, wrap="word", state=DISABLED)
        self.log_text.pack(fill=BOTH, expand=True)

    def _config_payload(self) -> dict[str, Any]:
        return {
            "buyer_base": self.buyer_base.get(),
            "buyer_key": _protect_secret(self.buyer_key.get()),
            "buyer_model": self.buyer_model.get(),
            "seller_base": self.seller_base.get(),
            "seller_key": _protect_secret(self.seller_key.get()),
            "seller_model": self.seller_model.get(),
            "proxy_url": self.proxy_url.get(),
            "shared_api": bool(self.shared_api.get()),
            "product_id": self.product_id.get(),
            "product_title": self.product_title.get(),
            "facts": self.facts.get("1.0", END),
            "sessions": self.sessions.get(),
            "turns": self.turns.get(),
            "buyer_output_tokens": self.buyer_output_tokens.get(),
            "seller_output_tokens": self.seller_output_tokens.get(),
            "organizer_output_tokens": self.organizer_output_tokens.get(),
            "output_dir": self.output_dir.get(),
            "resume_enabled": bool(self.resume_enabled.get()),
            "resume_file": self.resume_file.get(),
            "scenarios": self.scenarios.get("1.0", END),
            "organizer_enabled": bool(self.organizer_enabled.get()),
        }

    def save_config(self, silent: bool = False) -> None:
        try:
            path = _local_config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._config_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
            if not silent:
                self.write_log(f"配置已保存到本机用户目录：{path}")
        except Exception as exc:
            if not silent:
                messagebox.showerror("保存配置失败", str(exc))

    def _load_saved_config(self) -> None:
        path = _local_config_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fields = {
                "buyer_base": self.buyer_base,
                "buyer_model": self.buyer_model,
                "seller_base": self.seller_base,
                "seller_model": self.seller_model,
                "proxy_url": self.proxy_url,
                "product_id": self.product_id,
                "product_title": self.product_title,
                "sessions": self.sessions,
                "turns": self.turns,
                "buyer_output_tokens": self.buyer_output_tokens,
                "seller_output_tokens": self.seller_output_tokens,
                "organizer_output_tokens": self.organizer_output_tokens,
                "output_dir": self.output_dir,
                "resume_file": self.resume_file,
            }
            for key, entry in fields.items():
                if key in data and data[key] is not None:
                    entry.delete(0, END)
                    entry.insert(0, str(data[key]))
            for key, entry in (("buyer_key", self.buyer_key), ("seller_key", self.seller_key)):
                value = _unprotect_secret(str(data.get(key) or ""))
                if value:
                    entry.delete(0, END)
                    entry.insert(0, value)
            for key, entry in (("facts", self.facts), ("scenarios", self.scenarios)):
                if key in data and data[key] is not None:
                    entry.delete("1.0", END)
                    entry.insert("1.0", str(data[key]))
            self.shared_api.set(bool(data.get("shared_api", True)))
            self.organizer_enabled.set(bool(data.get("organizer_enabled", True)))
            self.resume_enabled.set(bool(data.get("resume_enabled", True)))
            self._toggle_shared()
            self.write_log("已加载上次保存的本机配置（API Key 已通过 Windows DPAPI 解密到内存）。")
        except Exception as exc:
            self.write_log(f"本机配置加载失败，将使用默认配置：{str(exc)[:300]}")

    def clear_saved_config(self) -> None:
        if not messagebox.askyesno("清除配置", "确定清除本机保存的 API 配置和任务参数吗？"):
            return
        path = _local_config_path()
        try:
            if path.exists():
                path.unlink()
            self.write_log("已清除本机保存配置；当前窗口中的内容仍保留。")
        except Exception as exc:
            messagebox.showerror("清除配置失败", str(exc))

    def _close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
        self.save_config(silent=True)
        self.root.destroy()

    def _field(self, parent: Frame, label: str, value: str, row: int, column: int, secret: bool = False) -> Entry:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 6), pady=4)
        entry = ttk.Entry(parent, show="•" if secret else "")
        entry.insert(0, value)
        entry.grid(row=row, column=column + 1, sticky="ew", pady=4)
        return entry

    def _toggle_shared(self) -> None:
        state = DISABLED if self.shared_api.get() else NORMAL
        self.buyer_base.configure(state=state)
        self.buyer_key.configure(state=state)
        self.buyer_model.configure(state=state)

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择对话包输出目录")
        if selected:
            self.output_dir.configure(state=NORMAL)
            self.output_dir.delete(0, END)
            self.output_dir.insert(0, selected)

    def choose_resume_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择已有对话包或断点文件",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if selected:
            self.resume_file.delete(0, END)
            self.resume_file.insert(0, selected)

    def write_log(self, message: str) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see(END)
        self.log_text.configure(state=DISABLED)

    def _config(self) -> JobConfig:
        proxy_url = self.proxy_url.get().strip()
        if proxy_url and not re.match(r"^https?://", proxy_url, flags=re.I):
            proxy_url = "http://" + proxy_url
        if proxy_url and not re.match(r"^https?://[^/]+", proxy_url, flags=re.I):
            raise ValueError("代理地址格式错误，请填写 http://127.0.0.1:10809")
        seller = ApiConfig(clean_base_url(self.seller_base.get()), self.seller_key.get().strip(), self.seller_model.get().strip(), proxy_url)
        if not seller.api_key:
            raise ValueError("请填写卖家 API Key")
        if not seller.model:
            raise ValueError("请填写卖家模型名称")
        if self.shared_api.get():
            buyer = seller
        else:
            buyer = ApiConfig(clean_base_url(self.buyer_base.get()), self.buyer_key.get().strip(), self.buyer_model.get().strip(), proxy_url)
            if not buyer.api_key or not buyer.model:
                raise ValueError("请完整填写买家 API 配置")
        scenarios = [line.strip() for line in self.scenarios.get("1.0", END).splitlines() if line.strip()]
        if not scenarios:
            raise ValueError("至少填写一个场景")
        try:
            sessions = max(1, min(MAX_SESSIONS_PER_SCENARIO, int(self.sessions.get().strip())))
            turns = max(1, min(MAX_TURNS_PER_SESSION, int(self.turns.get().strip())))
            buyer_output_tokens = max(32, min(4096, int(self.buyer_output_tokens.get().strip())))
            seller_output_tokens = max(32, min(4096, int(self.seller_output_tokens.get().strip())))
            organizer_output_tokens = max(128, min(8192, int(self.organizer_output_tokens.get().strip())))
        except ValueError as exc:
            raise ValueError("变体数、回合数和输出 Token 上限必须是整数") from exc
        # Muse 的 Responses max_output_tokens 包含推理预算；过低会返回
        # status=incomplete 且 output=[]。自动提高的是上限，不是固定消耗量。
        if buyer.model.startswith("muse-spark-"):
            buyer_output_tokens = max(buyer_output_tokens, 1024)
        if seller.model.startswith("muse-spark-"):
            seller_output_tokens = max(seller_output_tokens, 1536)
            organizer_output_tokens = max(organizer_output_tokens, 2048)
        return JobConfig(
            buyer=buyer,
            seller=seller,
            product_id=self.product_id.get().strip(),
            product_title=self.product_title.get().strip(),
            facts=self.facts.get("1.0", END).strip(),
            scenarios=scenarios,
            sessions_per_scenario=sessions,
            max_turns=turns,
            output_dir=Path(self.output_dir.get().strip()).expanduser().resolve(),
            use_organizer=self.organizer_enabled.get(),
            buyer_max_output_tokens=buyer_output_tokens,
            seller_max_output_tokens=seller_output_tokens,
            organizer_max_output_tokens=organizer_output_tokens,
            resume_enabled=self.resume_enabled.get(),
            resume_file=(Path(self.resume_file.get().strip()).expanduser().resolve() if self.resume_file.get().strip() else None),
        )

    def test_api(self) -> None:
        try:
            config = self._config()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.save_config(silent=True)
        self.write_log("开始测试卖家 API……")
        def run_test() -> None:
            try:
                # 测试请求使用最小兼容体：不发送 temperature/max_tokens，
                # 先排除部分网关对可选参数处理不兼容的问题。
                reply = OpenAICompatibleClient(config.seller, timeout=30).chat([
                    {"role": "system", "content": "只回复：API连接正常"},
                    {"role": "user", "content": "测试"},
                ], temperature=None)
                self.events.put(("log", f"API 测试成功：{reply[:120]}"))
            except Exception as exc:
                self.events.put(("log", f"API 测试失败：{exc}"))
        threading.Thread(target=run_test, daemon=True).start()

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self._config()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.save_config(silent=True)
        self.stop_event.clear()
        total_sessions = len(config.scenarios) * config.sessions_per_scenario
        estimated_calls = total_sessions * (config.max_turns * 2 + (1 if config.use_organizer else 0))
        self.start_button.configure(state=DISABLED)
        self.stop_button.configure(state=NORMAL)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.write_log(
            f"后台任务已启动：{total_sessions} 个会话，预计最多约 {estimated_calls} 次 API 调用。"
            "关闭窗口会中止当前任务，但已保存的 partial 文件仍会保留。"
        )
        if config.buyer.model.startswith("muse-spark-") or config.seller.model.startswith("muse-spark-"):
            self.write_log("已启用 Muse 兼容策略：reasoning=minimal，并自动保留足够 Token 给最终文本。")
        def work() -> None:
            try:
                result = Generator(config, lambda message: self.events.put(("log", message)), self.stop_event).run()
                self.events.put(("done", result))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.stop_button.configure(state=DISABLED)
        self.write_log("已请求停止，当前 API 调用结束后会停止并导出已完成结果。")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.write_log(str(value))
                elif kind == "done":
                    self.progress.stop()
                    self.start_button.configure(state=NORMAL)
                    self.stop_button.configure(state=DISABLED)
                    status = value.get("status")
                    self.write_log(f"任务{('完成' if status == 'completed' else '停止')}：模板 {len(value.get('templates') or [])} 条，输出目录：{self.output_dir.get()}")
                elif kind == "error":
                    self.progress.stop()
                    self.start_button.configure(state=NORMAL)
                    self.stop_button.configure(state=DISABLED)
                    self.write_log(f"任务异常终止：{value}")
                    messagebox.showerror("任务异常", str(value))
        except queue.Empty:
            pass
        self.root.after(150, self._poll_events)


def main() -> None:
    root = Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except Exception:
        pass
    app = App(root)
    if "--auto-start" in sys.argv[1:]:
        root.after(700, app.start)
    root.mainloop()


if __name__ == "__main__":
    main()
