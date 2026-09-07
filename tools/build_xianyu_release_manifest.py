#!/usr/bin/env python3
"""Create the JSON manifest consumed by the Windows Docker updater."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    registry = args.registry.rstrip("/")
    namespace = args.namespace.strip("/")
    services = {name: f"{registry}/{namespace}/xianyu-{name}:{args.tag}" for name in ("backend", "websocket", "scheduler", "frontend")}
    payload = {
        "product": "xianyu-rewrite",
        "version": args.version,
        "build_id": args.build_id,
        "release_date": datetime.now(timezone.utc).isoformat(),
        "image_registry": registry,
        "image_namespace": namespace,
        "image_tag": args.tag,
        "images": services,
        "mandatory": False,
        "notes": args.notes,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
