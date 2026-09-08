"""Promote a GHCR build while preserving every layer of the stable base image.

BuildKit may recompress imported parent layers when it pushes an image. Docker
then sees different blob digests and downloads the entire parent again. This
tool verifies the uncompressed diff-id chain, copies only layers added after
the stable base, and publishes a manifest that references the original base
descriptors already stored in the client-facing registry.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request


MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    )
)


def json_request(url: str, *, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def ghcr_token(repository: str) -> str:
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{repository}:pull"})
    payload = json_request(f"https://ghcr.io/token?{query}")
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise RuntimeError("GHCR did not return a pull token; confirm that the package is public")
    return str(token)


def fetch_manifest(registry: str, repository: str, reference: str, headers: dict[str, str] | None = None) -> dict:
    merged = {"Accept": MANIFEST_ACCEPT}
    merged.update(headers or {})
    return json_request(f"{registry}/v2/{repository}/manifests/{reference}", headers=merged)


def fetch_blob_json(registry: str, repository: str, digest: str, headers: dict[str, str] | None = None) -> dict:
    return json_request(f"{registry}/v2/{repository}/blobs/{digest}", headers=headers)


def local_blob_exists(registry: str, repository: str, digest: str) -> bool:
    request = urllib.request.Request(f"{registry}/v2/{repository}/blobs/{digest}", method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise


def copy_blob(source_registry: str, source_repository: str, destination_registry: str, destination_repository: str,
              descriptor: dict, source_headers: dict[str, str]) -> int:
    digest = str(descriptor["digest"])
    if local_blob_exists(destination_registry, destination_repository, digest):
        return 0
    source_url = f"{source_registry}/v2/{source_repository}/blobs/{digest}"
    with urllib.request.urlopen(urllib.request.Request(source_url, headers=source_headers), timeout=300) as response:
        payload = response.read()
    expected_size = int(descriptor.get("size", len(payload)))
    if len(payload) != expected_size:
        raise RuntimeError(f"blob size mismatch for {digest}: expected {expected_size}, received {len(payload)}")
    start = urllib.request.Request(
        f"{destination_registry}/v2/{destination_repository}/blobs/uploads/", data=b"", method="POST"
    )
    with urllib.request.urlopen(start, timeout=30) as response:
        location = response.headers.get("Location")
    if not location:
        raise RuntimeError(f"registry did not return an upload location for {digest}")
    upload_url = urllib.parse.urljoin(destination_registry, location)
    separator = "&" if "?" in upload_url else "?"
    finish = urllib.request.Request(
        f"{upload_url}{separator}{urllib.parse.urlencode({'digest': digest})}",
        data=payload,
        method="PUT",
        headers={"Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(finish, timeout=300) as response:
        if response.status not in (201, 202):
            raise RuntimeError(f"registry rejected {digest} with HTTP {response.status}")
    return len(payload)


def put_manifest(registry: str, repository: str, reference: str, manifest: dict) -> None:
    payload = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    media_type = str(manifest.get("mediaType") or "application/vnd.docker.distribution.manifest.v2+json")
    request = urllib.request.Request(
        f"{registry}/v2/{repository}/manifests/{reference}",
        data=payload,
        method="PUT",
        headers={"Content-Type": media_type},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status not in (201, 202):
            raise RuntimeError(f"registry rejected manifest with HTTP {response.status}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-repository", required=True, help="GHCR repository without registry hostname")
    parser.add_argument("--source-reference", required=True)
    parser.add_argument("--destination-repository", required=True)
    parser.add_argument("--base-reference", required=True)
    parser.add_argument("--target-reference", required=True)
    parser.add_argument("--destination-registry", default="http://127.0.0.1:5000")
    args = parser.parse_args()

    source_registry = "https://ghcr.io"
    source_headers = {"Authorization": f"Bearer {ghcr_token(args.source_repository)}"}
    source_manifest = fetch_manifest(source_registry, args.source_repository, args.source_reference, source_headers)
    base_manifest = fetch_manifest(
        args.destination_registry, args.destination_repository, args.base_reference
    )
    if "manifests" in source_manifest:
        candidates = [
            item
            for item in source_manifest["manifests"]
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(candidates) != 1:
            raise RuntimeError(f"expected one linux/amd64 image in source index, found {len(candidates)}")
        source_manifest = fetch_manifest(
            source_registry, args.source_repository, str(candidates[0]["digest"]), source_headers
        )
    if "layers" not in source_manifest or "layers" not in base_manifest:
        raise RuntimeError("source or base does not contain image layers")

    source_config = fetch_blob_json(
        source_registry, args.source_repository, str(source_manifest["config"]["digest"]), source_headers
    )
    base_config = fetch_blob_json(
        args.destination_registry, args.destination_repository, str(base_manifest["config"]["digest"])
    )
    source_diff_ids = list(source_config.get("rootfs", {}).get("diff_ids", []))
    base_diff_ids = list(base_config.get("rootfs", {}).get("diff_ids", []))
    base_layer_count = len(base_manifest["layers"])
    if len(base_diff_ids) != base_layer_count:
        raise RuntimeError("base config diff-id count does not match its manifest layer count")
    if source_diff_ids[:base_layer_count] != base_diff_ids:
        raise RuntimeError("source image is not based on the requested stable image")
    if len(source_manifest["layers"]) != len(source_diff_ids):
        raise RuntimeError("source config diff-id count does not match its manifest layer count")

    copied_bytes = copy_blob(
        source_registry,
        args.source_repository,
        args.destination_registry,
        args.destination_repository,
        source_manifest["config"],
        source_headers,
    )
    overlay_layers = list(source_manifest["layers"][base_layer_count:])
    for descriptor in overlay_layers:
        copied_bytes += copy_blob(
            source_registry,
            args.source_repository,
            args.destination_registry,
            args.destination_repository,
            descriptor,
            source_headers,
        )

    target_manifest = dict(source_manifest)
    target_manifest["layers"] = list(base_manifest["layers"]) + overlay_layers
    put_manifest(
        args.destination_registry, args.destination_repository, args.target_reference, target_manifest
    )
    print(
        f"Promoted {args.destination_repository}:{args.target_reference} from "
        f"{args.destination_repository}:{args.base_reference}; base_layers={base_layer_count}, "
        f"overlay_layers={len(overlay_layers)}, copied_bytes={copied_bytes}"
    )


if __name__ == "__main__":
    main()
