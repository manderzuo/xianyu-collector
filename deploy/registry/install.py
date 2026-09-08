"""Install the read-only public registry used by incremental client updates.

Run this script as root on the existing Gemstory server. Image writes stay on
the loopback-only registry endpoint; Nginx exposes only GET and HEAD requests
through the existing HTTPS virtual host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path


def configured_path(name: str, fallback: str) -> Path:
    value = os.environ.get(name, fallback).strip()
    if not value or any(char in value for char in "\r\n"):
        raise RuntimeError(f"{name} contains an invalid path")
    return Path(value)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run(args: list[str]) -> None:
    subprocess.run(args, check=True)


storage = configured_path("XIANYU_REGISTRY_DATA", "/var/lib/gemstory/xianyu-registry")
config = configured_path("XIANYU_REGISTRY_CONFIG", "/etc/docker/registry/config.yml")
nginx = configured_path("XIANYU_NGINX_CONFIG", "/etc/nginx/sites-available/filmcrew.conf")
stamp = datetime.now().strftime("%Y%m%d%H%M%S")

missing = [name for name in ("docker-registry", "skopeo") if not command_exists(name)]
if missing:
    run(["apt-get", "update"])
    run(["apt-get", "install", "-y", "--no-install-recommends", "docker-registry", "skopeo"])

run(["id", "docker-registry"])
storage.mkdir(parents=True, exist_ok=True)
shutil.chown(storage, "docker-registry", "docker-registry")
storage.chmod(0o750)
config.parent.mkdir(parents=True, exist_ok=True)

registry_config = f"""version: 0.1
log:
  fields:
    service: xianyu-registry
storage:
  filesystem:
    rootdirectory: {storage}
  delete:
    enabled: true
http:
  addr: 127.0.0.1:5000
  headers:
    X-Content-Type-Options: [nosniff]
health:
  storagedriver:
    enabled: true
    interval: 30s
    threshold: 3
"""

old_config = config.read_text(encoding="utf-8") if config.exists() else ""
if old_config != registry_config:
    if config.exists():
        shutil.copy2(config, Path(str(config) + ".bak-xianyu-" + stamp))
    config.write_text(registry_config, encoding="utf-8")

run(["systemctl", "daemon-reload"])
run(["systemctl", "enable", "docker-registry"])
run(["systemctl", "restart", "docker-registry"])

if not nginx.exists():
    raise RuntimeError(f"Nginx configuration does not exist: {nginx}")

start_marker = "    # BEGIN XIANYU REGISTRY\n"
end_marker = "    # END XIANYU REGISTRY\n"
nginx_block = """    # BEGIN XIANYU REGISTRY
    location ^~ /v2/ {
        limit_except GET {
            deny all;
        }
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 900s;
        proxy_buffering off;
        add_header Docker-Distribution-Api-Version "registry/2.0" always;
    }
    # END XIANYU REGISTRY
"""

old_nginx = nginx.read_text(encoding="utf-8")
has_start = start_marker in old_nginx
has_end = end_marker in old_nginx
if has_start != has_end:
    raise RuntimeError("Incomplete Xianyu registry markers in Nginx configuration")
if has_start:
    start = old_nginx.index(start_marker)
    end = old_nginx.index(end_marker, start) + len(end_marker)
    updated_nginx = old_nginx[:start] + nginx_block + old_nginx[end:]
else:
    if "location ^~ /v2/" in old_nginx or "location /v2/" in old_nginx:
        raise RuntimeError("An unmanaged /v2/ Nginx location already exists")
    marker = "    location / {\n"
    if old_nginx.count(marker) != 1:
        raise RuntimeError("Unable to locate the HTTPS root location in Nginx configuration")
    updated_nginx = old_nginx.replace(marker, nginx_block + "\n" + marker, 1)

if updated_nginx != old_nginx:
    backup = Path(str(nginx) + ".bak-xianyu-registry-" + stamp)
    shutil.copy2(nginx, backup)
    nginx.write_text(updated_nginx, encoding="utf-8")
    if subprocess.run(["nginx", "-t"]).returncode:
        shutil.copy2(backup, nginx)
        raise RuntimeError("Invalid Nginx configuration; restored backup")
    run(["systemctl", "reload", "nginx"])

with urllib.request.urlopen("http://127.0.0.1:5000/v2/", timeout=10) as response:
    if response.status != 200:
        raise RuntimeError(f"Registry health check returned HTTP {response.status}")

print(f"Xianyu incremental registry installed at {storage}")
