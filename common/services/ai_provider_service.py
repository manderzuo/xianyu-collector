# -*- coding: utf-8 -*-
"""统一的 AI 服务商调用适配器。

账号设置页、模型列表、连接测试和实时自动回复必须走同一套协议适配，
避免“页面显示已配置，但后台实际不会调用”的假成功。
"""
from __future__ import annotations

from typing import Any

import httpx


DEFAULT_AI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_AI_PROVIDER_TYPE = "openai_compatible"
VALID_AI_PROVIDER_TYPES = {"openai_compatible", "anthropic", "gemini", "dashscope_app"}
AI_PROVIDER_DEFAULT_BASE_URLS = {
    "openai_compatible": DEFAULT_AI_BASE_URL,
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "dashscope_app": "https://dashscope.aliyuncs.com/api/v1/apps/{app_id}/completion",
}
AI_PROVIDER_NAMES = {
    "openai_compatible": "OpenAI兼容",
    "anthropic": "Anthropic Claude",
    "gemini": "Google Gemini",
    "dashscope_app": "DashScope应用",
}


def clean_ai_text(value: Any) -> str:
    return str(value or "").replace("\r", "").replace("\n", "").strip()


def normalize_ai_provider_type(provider_type: Any = None, base_url: Any = "", model_name: Any = "") -> str:
    provider = clean_ai_text(provider_type).lower().replace("-", "_")
    aliases = {
        "openai": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "openai兼容": "openai_compatible",
        "dashscope": "openai_compatible",
        "dashscope_compatible": "openai_compatible",
        "qwen": "openai_compatible",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "gemini": "gemini",
        "google_gemini": "gemini",
        "dashscope_app": "dashscope_app",
        "dashscope应用": "dashscope_app",
    }
    if provider in aliases:
        return aliases[provider]
    if provider in VALID_AI_PROVIDER_TYPES:
        return provider
    base = clean_ai_text(base_url).lower()
    model = clean_ai_text(model_name).lower()
    if "generativelanguage.googleapis.com" in base:
        return "gemini"
    if "api.anthropic.com" in base:
        return "anthropic"
    if "/apps/" in base and "dashscope.aliyuncs.com" in base:
        return "dashscope_app"
    if "gemini" in model:
        return "gemini"
    if "claude" in model:
        return "anthropic"
    return DEFAULT_AI_PROVIDER_TYPE


def read_ai_enabled(settings: dict[str, Any] | None) -> bool:
    payload = settings or {}
    value = payload.get("ai_enabled")
    if value is None:
        value = payload.get("enabled", False)
    return bool(value)


def normalize_ai_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(settings or {})
    provider = normalize_ai_provider_type(
        payload.get("provider_type"), payload.get("base_url"), payload.get("model_name")
    )
    payload["provider_type"] = provider
    payload["api_key"] = clean_ai_text(payload.get("api_key"))
    payload["base_url"] = clean_ai_text(payload.get("base_url")) or AI_PROVIDER_DEFAULT_BASE_URLS[provider]
    payload["model_name"] = clean_ai_text(payload.get("model_name")) or "qwen-plus"
    return payload


def get_ai_settings_missing_fields(settings: dict[str, Any] | None) -> list[str]:
    payload = settings or {}
    provider = normalize_ai_provider_type(
        payload.get("provider_type"), payload.get("base_url"), payload.get("model_name")
    )
    missing: list[str] = []
    if not clean_ai_text(payload.get("base_url")):
        missing.append("API地址")
    if not clean_ai_text(payload.get("api_key")):
        missing.append("API Key")
    if not clean_ai_text(payload.get("model_name")):
        missing.append("模型名称")
    if provider == "dashscope_app" and ("{app_id}" in clean_ai_text(payload.get("base_url")) or "/apps/" not in clean_ai_text(payload.get("base_url"))):
        missing.append("DashScope应用地址")
    return missing


def normalize_openai_base_url(base_url: Any) -> str:
    value = clean_ai_text(base_url) or DEFAULT_AI_BASE_URL
    value = value.rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value


def build_openai_url(base_url: Any, path: str) -> str:
    return f"{normalize_openai_base_url(base_url)}/{path.lstrip('/')}"


def build_anthropic_url(base_url: Any, path: str) -> str:
    value = (clean_ai_text(base_url) or AI_PROVIDER_DEFAULT_BASE_URLS["anthropic"]).rstrip("/")
    return f"{value}/{path.lstrip('/')}" if value.endswith("/v1") else f"{value}/v1/{path.lstrip('/')}"


def build_gemini_url(base_url: Any, path: str) -> str:
    value = (clean_ai_text(base_url) or AI_PROVIDER_DEFAULT_BASE_URLS["gemini"]).rstrip("/")
    return f"{value}/{path.lstrip('/')}" if value.endswith(("/v1", "/v1beta")) else f"{value}/v1beta/{path.lstrip('/')}"


def provider_name(provider_type: Any, base_url: Any = "", model_name: Any = "") -> str:
    provider = normalize_ai_provider_type(provider_type, base_url, model_name)
    return AI_PROVIDER_NAMES.get(provider, "OpenAI兼容")


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or f"HTTP {response.status_code}")[:500]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or error)[:500]
        if error:
            return str(error)[:500]
        for key in ("message", "msg", "detail"):
            if body.get(key):
                return str(body[key])[:500]
    return str(body)[:500]


def _ensure_success(response: httpx.Response, provider: str) -> None:
    if 200 <= response.status_code < 300:
        return
    raise RuntimeError(f"{provider}返回HTTP {response.status_code}: {_error_text(response)}")


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # 兼容 OpenAI 新格式、Anthropic block 以及部分代理的
                # {type: ..., text: ...} 数组响应。
                item_text = _content_text(
                    item.get("text")
                    or item.get("output_text")
                    or item.get("content")
                    or item.get("value")
                )
                if item_text:
                    parts.append(item_text)
        return "".join(parts).strip()
    if isinstance(value, dict):
        for key in ("text", "output_text", "content", "answer", "reply"):
            item_text = _content_text(value.get(key))
            if item_text:
                return item_text
    return ""


def _openai_reply_text(data: Any) -> str:
    """解析 OpenAI 兼容服务常见的非流式回复形态。

    除标准的 choices[0].message.content 外，部分网关会返回 completion
    风格的 choices[0].text，或在 data/output/result 中再包一层。模型列表
    接口能成功并不代表响应一定严格遵循同一种包装格式，因此这里集中兼容。
    """
    if isinstance(data, dict):
        choices = data.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    result = _content_text(message.get("content"))
                    if result:
                        return result
                for key in ("text", "output_text", "content"):
                    result = _content_text(choice.get(key))
                    if result:
                        return result

        for key in ("output_text", "text", "answer", "reply", "response"):
            result = _content_text(data.get(key))
            if result:
                return result

        for key in ("output", "data", "result"):
            nested = data.get(key)
            if isinstance(nested, (dict, list)):
                result = _openai_reply_text(nested)
                if result:
                    return result
    elif isinstance(data, list):
        for item in data:
            result = _openai_reply_text(item)
            if result:
                return result
    return ""


def _model_options(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = clean_ai_text(item.get("id") or item.get("name") or item.get("model"))
        if model_id.startswith("models/"):
            model_id = model_id.split("/", 1)[1]
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append({"id": model_id, "name": clean_ai_text(item.get("display_name") or item.get("displayName") or item.get("name")) or model_id})
    return result


async def fetch_ai_model_list(provider_type: Any, base_url: Any, api_key: Any) -> list[dict[str, str]]:
    settings = normalize_ai_settings({"provider_type": provider_type, "base_url": base_url, "api_key": api_key})
    if not settings["api_key"]:
        raise ValueError("请先填写API Key")
    provider = settings["provider_type"]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        if provider == "anthropic":
            response = await client.get(build_anthropic_url(settings["base_url"], "/models"), headers={"x-api-key": settings["api_key"], "anthropic-version": "2023-06-01"})
            _ensure_success(response, provider_name(provider))
            return _model_options((response.json() or {}).get("data"))
        if provider == "gemini":
            response = await client.get(build_gemini_url(settings["base_url"], "/models"), params={"key": settings["api_key"]})
            _ensure_success(response, provider_name(provider))
            return _model_options((response.json() or {}).get("models"))
        if provider == "dashscope_app":
            raise ValueError("DashScope应用API不支持自动获取模型列表，请手动填写模型名称")
        response = await client.get(build_openai_url(settings["base_url"], "/models"), headers={"Authorization": f"Bearer {settings['api_key']}"})
        _ensure_success(response, provider_name(provider))
        body = response.json()
        return _model_options(body.get("data") or body.get("models") if isinstance(body, dict) else body)


async def generate_ai_reply(
    provider_type: Any,
    base_url: Any,
    api_key: Any,
    model_name: Any,
    messages: list[dict[str, str]],
    *,
    # 推理模型会先消耗一部分 token 生成 reasoning_content；512 在客服
    # 提示词较长时仍可能在生成最终 content 前结束，表现为“接口成功但
    # 返回空回复”。给推理模型留出完整的推理与最终答复空间。
    max_tokens: int = 1024,
    temperature: float = 0.4,
) -> str:
    raw = {"provider_type": provider_type, "base_url": base_url, "api_key": api_key, "model_name": model_name}
    missing = get_ai_settings_missing_fields(raw)
    if missing:
        raise ValueError(f"AI配置未填写完整，请先补全：{'、'.join(missing)}")
    settings = normalize_ai_settings(raw)
    provider = settings["provider_type"]
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        if provider == "anthropic":
            system = "\n".join(item["content"] for item in messages if item.get("role") == "system")
            chat_messages = [item for item in messages if item.get("role") != "system"]
            response = await client.post(
                build_anthropic_url(settings["base_url"], "/messages"),
                headers={"x-api-key": settings["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": settings["model_name"], "max_tokens": max_tokens, "temperature": temperature, "system": system, "messages": chat_messages},
            )
            _ensure_success(response, provider_name(provider))
            content = (response.json() or {}).get("content", [])
            result = _content_text(next((item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"), ""))
        elif provider == "gemini":
            system = "\n".join(item["content"] for item in messages if item.get("role") == "system")
            user_text = "\n".join(item["content"] for item in messages if item.get("role") != "system")
            response = await client.post(
                build_gemini_url(settings["base_url"], f"/models/{settings['model_name']}:generateContent"),
                params={"key": settings["api_key"]},
                json={"contents": [{"role": "user", "parts": [{"text": user_text}]}], "systemInstruction": {"parts": [{"text": system}]} if system else None, "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}},
            )
            _ensure_success(response, provider_name(provider))
            result = _content_text((((response.json() or {}).get("candidates") or [{}])[0].get("content") or {}).get("parts", [{}])[0].get("text"))
        elif provider == "dashscope_app":
            base = settings["base_url"]
            if "{app_id}" in base or "/apps/" not in base:
                raise ValueError("DashScope应用API地址中未找到有效 app_id")
            prompt = "\n\n".join(item["content"] for item in messages)
            response = await client.post(
                base,
                headers={"Authorization": f"Bearer {settings['api_key']}", "content-type": "application/json"},
                json={"input": {"prompt": prompt}, "parameters": {"max_tokens": max_tokens, "temperature": temperature}, "debug": {}},
            )
            _ensure_success(response, provider_name(provider))
            result = _content_text(((response.json() or {}).get("output") or {}).get("text"))
        else:
            response = await client.post(
                build_openai_url(settings["base_url"], "/chat/completions"),
                headers={"Authorization": f"Bearer {settings['api_key']}", "content-type": "application/json"},
                json={"model": settings["model_name"], "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            )
            _ensure_success(response, provider_name(provider))
            result = _openai_reply_text(response.json())
    if not result:
        raise RuntimeError(f"{provider_name(provider)}返回空回复")
    return result.replace("\r", "").replace("\n", " ").strip()[:500]


async def test_ai_connection(provider_type: Any, base_url: Any, api_key: Any, model_name: Any) -> str:
    return await generate_ai_reply(
        provider_type,
        base_url,
        api_key,
        model_name,
        [
            {"role": "system", "content": "你是连接测试助手，只回复四个字：测试成功。"},
            {"role": "user", "content": "请回复测试成功"},
        ],
        # 连接测试也必须给推理模型留出生成最终短回复的空间。
        max_tokens=1024,
        temperature=0,
    )
