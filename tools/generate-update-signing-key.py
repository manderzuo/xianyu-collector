#!/usr/bin/env python3
"""Generate the release signing key pair used by the Windows updater.

The private PEM is intended for a GitHub Actions secret and must never be
committed or copied into a client package. The public XML is safe to ship.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def unsigned_base64(value: int) -> str:
    size = max(1, (value.bit_length() + 7) // 8)
    return base64.b64encode(value.to_bytes(size, "big")).decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    private_path = output_dir / "update-signing-private.pem"
    public_path = output_dir / "update-signing-public.xml"
    if output_dir.exists() and any(output_dir.iterdir()) and not args.force:
        raise SystemExit(f"Output directory is not empty: {output_dir}; use --force to replace it.")
    output_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_numbers = key.public_key().public_numbers()
    public_xml = (
        "<RSAKeyValue><Modulus>"
        + unsigned_base64(public_numbers.n)
        + "</Modulus><Exponent>"
        + unsigned_base64(public_numbers.e)
        + "</Exponent></RSAKeyValue>\n"
    )
    private_path.write_bytes(private_pem)
    public_path.write_text(public_xml, encoding="utf-8")
    private_path.chmod(0o600)
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = hashlib.sha256(public_der).hexdigest()
    print(f"Private key: {private_path}")
    print(f"Public key:  {public_path}")
    print(f"Public key SHA-256 fingerprint: {fingerprint}")
    print("Copy only the private PEM contents to the UPDATE_SIGNING_PRIVATE_KEY GitHub secret.")
    print("Copy the public XML into deploy/update-signing-public-key.xml for client packages.")


if __name__ == "__main__":
    main()
