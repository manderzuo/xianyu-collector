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
    parser.add_argument("--package-url", default="")
    parser.add_argument("--package-sha256", default="")
    parser.add_argument("--package-signature", default="")
    parser.add_argument("--client-launcher-sha256", default="")
    parser.add_argument("--client-frontend-index-sha256", default="")
    parser.add_argument("--min-launcher-version", default="1.0.0")
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--image-artifacts-json", default="")
    parser.add_argument("--signature-url", default="")
    parser.add_argument("--signature-algorithm", default="RSA-SHA256")
    parser.add_argument("--runtime-images-required", choices=("true", "false"), default="true")
    parser.add_argument("--runtime-images-deferred", choices=("true", "false"), default="false")
    args = parser.parse_args()

    registry = args.registry.rstrip("/")
    namespace = args.namespace.strip("/")
    services = {name: f"{registry}/{namespace}/xianyu-{name}:{args.tag}" for name in ("backend", "websocket", "scheduler", "frontend")}
    image_artifacts = []
    if args.image_artifacts_json:
        artifact_path = Path(args.image_artifacts_json)
        loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
        image_artifacts = loaded.get("artifacts", []) if isinstance(loaded, dict) else loaded
        if not isinstance(image_artifacts, list):
            raise SystemExit("--image-artifacts-json must contain a JSON array or an object with an artifacts array")
    payload = {
        "product": "xianyu-rewrite",
        "version": args.version,
        "build_id": args.build_id,
        "release_date": datetime.now(timezone.utc).isoformat(),
        "image_registry": registry,
        "image_namespace": namespace,
        "image_tag": args.tag,
        "images": services,
        "image_artifacts": image_artifacts,
        "runtime_images_required": args.runtime_images_required == "true",
        "runtime_images_deferred": args.runtime_images_deferred == "true",
        "update_protocol": 3,
        "signature": {
            "url": args.signature_url or None,
            "algorithm": args.signature_algorithm if args.signature_url else None,
            "encoding": "base64" if args.signature_url else None,
        },
        "client": {
            "min_launcher_version": args.min_launcher_version,
            "package_url": args.package_url or None,
            "package_sha256": args.package_sha256 or None,
            "package_signature": args.package_signature or None,
            "launcher_sha256": args.client_launcher_sha256 or None,
            "frontend_index_sha256": args.client_frontend_index_sha256 or None,
        },
        "mandatory": bool(args.mandatory),
        "notes": args.notes,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
