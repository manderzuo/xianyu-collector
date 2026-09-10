# -*- coding: utf-8 -*-
"""闲鱼客服对话包离线蒸馏器。

只读取已有 dialogue_pack JSON，不调用 API，不修改原始生成结果。
输出生产候选、人工审核集、关键词导入文件和可追溯报告。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMAT = "xianyu-local-dialogue-pack/distilled-v1"
PLACEHOLDER_RE = re.compile(r"待填写|暂未提供|未提供|请填写实际|undefined|null", re.I)
META_RE = re.compile(r"我是?AI|人工智能|提示词|模拟对话|数据集|system message", re.I)
TRAILING_INCOMPLETE_RE = re.compile(r"[，、：；/（(]$")
ALLOWED_VARIABLES = {
    "buyer_name", "item_title", "price", "original_price", "stock", "sellable_stock",
    "platform_stock", "card_stock", "delivery_method", "delivery_time", "order_id",
    "DELIVERY_CONTENT",
}
GENERIC_KEYWORDS = {
    "你好", "您好", "在吗", "有货", "没货", "多少", "多少钱", "什么", "怎么", "可以吗",
    "行吗", "可以", "谢谢", "好的", "收到", "没有", "发货", "客服", "链接", "地址",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def scenario_group(value: str) -> str:
    for prefix, group in (
        ("售前咨询", "售前咨询"),
        ("售中下单", "售中下单"),
        ("售后处理", "售后处理"),
        ("沟通理解", "沟通理解"),
        ("风险拦截", "风险拦截"),
        ("人工接管", "人工接管"),
        ("交付", "交付"),
        ("库存", "库存"),
    ):
        if prefix in value:
            return group
    return "其他"


def source_scenario(template: dict[str, Any], input_scenarios: list[str]) -> tuple[str, bool]:
    source_key = str(template.get("source_session_key") or "")
    if "\x1f" in source_key:
        candidate = source_key.split("\x1f", 1)[0].strip()
        if candidate in input_scenarios:
            return candidate, True
    text = f"{template.get('scene') or ''} {template.get('intent') or ''}"
    group = scenario_group(text)
    return f"{group}（未关联具体原始场景）", False


def referenced_variables(reply: str) -> set[str]:
    return {name for name in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", reply) if name in ALLOWED_VARIABLES}


def clean_keywords(value: Any) -> list[str]:
    result: list[str] = []
    values = value if isinstance(value, list) else str(value or "").splitlines()
    for raw in values:
        keyword = re.sub(r"\s+", "", str(raw or "").strip())
        keyword = keyword.strip("，。！？、；：,!?;:()（）[]【】")
        if 1 < len(keyword) <= 30 and keyword not in result:
            result.append(keyword)
    return result[:20]


def quality_score(template: dict[str, Any], keywords: list[str], keyword_replies: dict[str, set[str]]) -> tuple[int, list[str]]:
    reply = compact_text(template.get("reply"))
    score = 50
    reasons: list[str] = []
    if template.get("source_session_key"):
        score += 8
    if template.get("source") == "synthetic_fallback":
        score -= 20
        reasons.append("fallback")
    if template.get("needs_human"):
        score -= 8
        reasons.append("needs_human")
    if 12 <= len(reply) <= 120:
        score += 12
    elif len(reply) < 8:
        score -= 15
        reasons.append("reply_too_short")
    elif len(reply) > 180:
        score -= 8
        reasons.append("reply_long")
    if TRAILING_INCOMPLETE_RE.search(reply):
        score -= 12
        reasons.append("trailing_incomplete")
    if META_RE.search(reply):
        score -= 50
        reasons.append("meta_leak")
    if PLACEHOLDER_RE.search(reply):
        score -= 50
        reasons.append("unfilled_fact")
    refs = referenced_variables(reply)
    declared = {str(x) for x in template.get("variables") or []}
    if declared - refs:
        score -= min(5, len(declared - refs))
        reasons.append("unused_declared_variables")
    unique_keywords = sum(1 for keyword in keywords if len(keyword_replies.get(keyword, set())) == 1)
    score += min(18, unique_keywords * 3)
    if not unique_keywords:
        score -= 12
        reasons.append("ambiguous_keywords")
    return max(0, min(100, score)), reasons


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def load_latest_package(root: Path) -> Path:
    candidates = [p for p in root.glob("dialogue_pack_*.json") if p.is_file() and "distilled" not in p.parts]
    if not candidates:
        raise FileNotFoundError(f"未找到生成包：{root}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def atomic_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def distill(input_path: Path, output_dir: Path, max_per_scenario: int) -> dict[str, Any]:
    source = json.loads(input_path.read_text(encoding="utf-8"))
    input_scenarios = [str(x) for x in source.get("generation", {}).get("scenarios") or []]
    completed_keys = {
        f"{item.get('scenario')}\x1f{item.get('session_index')}"
        for item in source.get("sessions") or []
        if item.get("status") == "completed"
    }
    raw_templates = [item for item in source.get("templates") or [] if isinstance(item, dict)]
    keyword_replies: dict[str, set[str]] = defaultdict(set)
    prepared: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}

    for index, raw in enumerate(raw_templates):
        reply = compact_text(raw.get("reply"))
        keywords = clean_keywords(raw.get("keywords"))
        linked_key = str(raw.get("source_session_key") or "")
        if linked_key and linked_key not in completed_keys:
            quarantine.append({"reason": "source_session_not_completed", "source_index": index, "template": raw})
            continue
        if not reply or not keywords:
            quarantine.append({"reason": "empty_reply_or_keywords", "source_index": index, "template": raw})
            continue
        scenario, linked = source_scenario(raw, input_scenarios)
        group = scenario_group(scenario)
        reply_key = normalize_text(reply)
        intent_key = normalize_text(raw.get("intent") or raw.get("scene") or scenario)
        item = dict(raw)
        item.update({
            "scenario": scenario,
            "scenario_group": group,
            "keywords": keywords,
            "reply": reply,
            "variables": sorted(referenced_variables(reply)),
            "source_linked": linked,
            "source_index": index,
        })
        key = (scenario, intent_key, reply_key)
        current = merged.get(key)
        if current is None:
            merged[key] = item
        else:
            current["keywords"] = clean_keywords((current.get("keywords") or []) + keywords)
            if linked and not current.get("source_linked"):
                current["source_linked"] = True
                current["source_session_key"] = item.get("source_session_key")

    prepared = list(merged.values())
    for item in prepared:
        reply_key = normalize_text(item.get("reply"))
        for keyword in item.get("keywords") or []:
            keyword_replies[keyword].add(reply_key)

    for item in prepared:
        score, reasons = quality_score(item, item.get("keywords") or [], keyword_replies)
        item["distillation_score"] = score
        item["distillation_reasons"] = reasons
        if PLACEHOLDER_RE.search(str(item.get("reply") or "")) or META_RE.search(str(item.get("reply") or "")):
            quarantine.append({"reason": "unfilled_fact_or_meta_leak", "source_index": item.get("source_index"), "template": item})

    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in prepared:
        by_scenario[str(item.get("scenario") or "其他（未关联具体原始场景）")].append(item)

    selected: list[dict[str, Any]] = []
    for scenario, candidates in by_scenario.items():
        available = [
            item for item in candidates
            if not PLACEHOLDER_RE.search(str(item.get("reply") or ""))
            and not META_RE.search(str(item.get("reply") or ""))
            and item.get("source_linked")
            and not item.get("needs_human")
        ]
        chosen: list[dict[str, Any]] = []
        used_intents: set[str] = set()
        used_replies: set[str] = set()
        while available and len(chosen) < max_per_scenario:
            available.sort(
                key=lambda item: (
                    int(item.get("distillation_score") or 0)
                    + (12 if normalize_text(item.get("intent")) not in used_intents else 0)
                    + (4 if normalize_text(item.get("reply")) not in used_replies else 0),
                    len(item.get("keywords") or []),
                ),
                reverse=True,
            )
            item = available.pop(0)
            chosen.append(item)
            used_intents.add(normalize_text(item.get("intent")))
            used_replies.add(normalize_text(item.get("reply")))
        selected.extend(chosen)

    production: list[dict[str, Any]] = []
    import_rows: list[dict[str, Any]] = []
    non_importable: list[dict[str, Any]] = []
    for item in selected:
        import_keywords = [
            keyword for keyword in item.get("keywords") or []
            if len(keyword) >= 3
            and keyword not in GENERIC_KEYWORDS
            and len(keyword_replies.get(keyword, set())) == 1
        ]
        distilled = dict(item)
        distilled["import_keywords"] = import_keywords
        distilled["importable"] = bool(import_keywords)
        production.append(distilled)
        if import_keywords:
            import_rows.append({
                "keyword": "\n".join(import_keywords),
                "reply": item["reply"],
                "item_id": str(item.get("item_id") or ""),
                "type": "text",
                "status": "active",
                "priority": int(item.get("priority") or 50),
                "scene": item.get("scenario"),
                "needs_human": False,
                "variables": item.get("variables") or [],
                "distillation_score": item.get("distillation_score"),
            })
        else:
            non_importable.append({"reason": "no_unique_keyword", "template": distilled})

    for item in prepared:
        if not item.get("source_linked") or item.get("needs_human"):
            reason = "legacy_unlinked_or_human_review"
            quarantine.append({"reason": reason, "source_index": item.get("source_index"), "template": item})

    status_counts = Counter(str(x.get("status")) for x in source.get("sessions") or [])
    ambiguous_keywords = sorted(
        ((keyword, len(replies)) for keyword, replies in keyword_replies.items() if len(replies) > 1),
        key=lambda x: x[1], reverse=True,
    )
    report = {
        "created_at": now_iso(),
        "source_file": str(input_path.resolve()),
        "source_sha256_prefix": fingerprint(source),
        "source_status": source.get("status"),
        "source_sessions": len(source.get("sessions") or []),
        "source_session_status": dict(status_counts),
        "source_templates": len(raw_templates),
        "deduplicated_templates": len(prepared),
        "production_templates": len(production),
        "production_import_rows": len(import_rows),
        "review_items": len(quarantine) + len(non_importable),
        "distilled_scenarios": len(by_scenario),
        "max_per_scenario": max_per_scenario,
        "ambiguous_keywords": len(ambiguous_keywords),
        "top_ambiguous_keywords": ambiguous_keywords[:30],
        "placeholder_replies": sum(1 for item in prepared if PLACEHOLDER_RE.search(str(item.get("reply") or ""))),
        "needs_human_templates": sum(1 for item in prepared if item.get("needs_human")),
        "unlinked_templates": sum(1 for item in prepared if not item.get("source_linked")),
        "non_importable_selected": len(non_importable),
        "recommendation": "先审核 distilled_review.json，再导入 distilled_keywords_import.json；不要全量导入原始 keywords_import 文件。",
    }
    package = {
        "format": FORMAT,
        "status": "review_required",
        "created_at": now_iso(),
        "source": {"file": str(input_path.resolve()), "sha256_prefix": report["source_sha256_prefix"]},
        "statistics": report,
        "templates": production,
        "review_required": True,
        "warning": "蒸馏结果仍是 AI 合成候选，启用前必须按真实商品事实审核。",
    }
    review = {
        "format": FORMAT + "/review",
        "created_at": now_iso(),
        "source": package["source"],
        "failed_sessions": [
            {"scenario": x.get("scenario"), "session_index": x.get("session_index"), "status": x.get("status"), "error": x.get("error")}
            for x in source.get("sessions") or [] if x.get("status") != "completed"
        ],
        "items": quarantine + non_importable,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(output_dir / "distilled_pack.json", package)
    atomic_write(output_dir / "distilled_keywords_import.json", import_rows)
    atomic_write(output_dir / "distilled_review.json", review)
    atomic_write(output_dir / "distillation_report.json", report)
    with (output_dir / "distilled_templates.jsonl").open("w", encoding="utf-8") as handle:
        for item in production:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="离线蒸馏已有闲鱼客服对话包")
    parser.add_argument("--input", type=Path, help="输入 dialogue_pack JSON；默认读取 dialogue_packs 下最新包")
    parser.add_argument("--output-dir", type=Path, default=Path("dialogue_packs/distilled"))
    parser.add_argument("--max-per-scenario", type=int, default=10)
    args = parser.parse_args()
    input_path = args.input or load_latest_package(Path("dialogue_packs"))
    report = distill(input_path.resolve(), args.output_dir.resolve(), max(1, min(50, args.max_per_scenario)))
    print(json.dumps({
        "source": report["source_file"],
        "production_templates": report["production_templates"],
        "import_rows": report["production_import_rows"],
        "review_items": report["review_items"],
        "failed_sessions": report["source_session_status"].get("failed", 0),
        "output_dir": str(args.output_dir.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
