"""Run with sudo on the existing Gemstory host, after uploading this directory."""
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

def configured_path(name: str, fallback: str) -> Path:
    value = os.environ.get(name, fallback).strip()
    if not value or any(char in value for char in '\r\n'):
        raise RuntimeError(f'{name} contains an invalid path')
    return Path(value)


# Defaults preserve the existing deployment.  Environment overrides make the
# same installer usable during a server migration without editing source.
root = configured_path('XIANYU_AUTH_ROOT', '/opt/gemstory/xianyu-auth')
data = configured_path('XIANYU_AUTH_DATA', '/var/lib/gemstory/xianyu-auth')
nginx = configured_path('XIANYU_NGINX_CONFIG', '/etc/nginx/sites-available/filmcrew.conf')
collector_db = configured_path('XIANYU_COLLECTOR_AUTH_DB', '/var/lib/gemstory/collector-auth/server.db')
stamp = datetime.now().strftime('%Y%m%d%H%M%S')
subprocess.run(['id', 'xianyu-auth'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 or subprocess.run(['useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', 'xianyu-auth'], check=True)
root.mkdir(parents=True, exist_ok=True)
data.mkdir(parents=True, exist_ok=True)
session_key = data / 'session.key'
if not session_key.exists():
    session_key.write_text(secrets.token_urlsafe(48) + '\n')
    session_key.chmod(0o600)
diagnostic_key = data / 'diagnostic.key'
if not diagnostic_key.exists():
    diagnostic_key.write_text(secrets.token_urlsafe(48) + '\n')
    diagnostic_key.chmod(0o600)
session_env = data / 'session.env'
session_env.write_text(
    'XIANYU_CLOUD_SESSION_KEY=' + session_key.read_text().strip() + '\n'
    'XIANYU_DIAGNOSTIC_STORAGE_KEY=' + diagnostic_key.read_text().strip() + '\n'
    'XIANYU_DIAGNOSTICS_RETENTION_DAYS=30\n'
)
session_env.chmod(0o600)
db = data / 'server.db'
if db.exists():
    with sqlite3.connect(db) as source, sqlite3.connect(data / ('backup-' + stamp + '.db')) as target:
        source.backup(target)
for name in ['server.py', 'auth_store.py', 'diagnostic_store.py', 'admin.html']:
    target = root / name
    if target.exists():
        shutil.copy2(target, root / (name + '.bak-' + stamp))
    shutil.copy2(Path(__file__).parent / name, target)
# Ubuntu 24 marks the system interpreter as externally managed (PEP 668).
# The service deliberately runs with /usr/bin/python3, so use the distro
# package instead of mutating that interpreter with pip.
crypto_check = subprocess.run(
    ['/usr/bin/python3', '-c', 'from cryptography.fernet import Fernet'],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
if crypto_check.returncode != 0:
    subprocess.run(['apt-get', 'update'], check=True)
    subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends', 'python3-cryptography'], check=True)
# Only seed the independent administrator on first install, preserving the
# collector administrator's password hash without printing or copying its users.
if not db.exists():
    import sys
    sys.path.insert(0, str(root))
    from auth_store import AuthStore
    os.environ['XIANYU_BOOTSTRAP_PASSWORD'] = secrets.token_urlsafe(48)
    AuthStore(str(db))
    with sqlite3.connect(f'file:{collector_db}?mode=ro', uri=True) as source:
        row = source.execute("SELECT password_hash FROM app_users WHERE username='admin' AND role='admin'").fetchone()
    if not row:
        raise RuntimeError('Existing administrator not found')
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE app_users SET password_hash=? WHERE username='admin'", (row[0],))
shutil.chown(data, 'xianyu-auth', 'xianyu-auth')
for p in data.iterdir():
    shutil.chown(p, 'xianyu-auth', 'xianyu-auth')
    if p.is_dir():
        p.chmod(0o700)
        for child in p.rglob('*'):
            shutil.chown(child, 'xianyu-auth', 'xianyu-auth')
            child.chmod(0o700 if child.is_dir() else 0o600)
    else:
        p.chmod(0o600)
data.chmod(0o700)
unit = Path('/etc/systemd/system/xianyu-auth.service')
unit.write_text(f'''[Unit]
Description=Xianyu registration approval API
After=network-online.target
[Service]
User=xianyu-auth
Group=xianyu-auth
WorkingDirectory={root}
EnvironmentFile={data}/session.env
ExecStart=/usr/bin/python3 {root}/server.py --db {data}/server.db --port 8766
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths={data}
[Install]
WantedBy=multi-user.target
''')
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'xianyu-auth'], check=True)
subprocess.run(['systemctl', 'restart', 'xianyu-auth'], check=True)
old = nginx.read_text()
marker = '    location ^~ /api/collector/'
block = '''    location ^~ /api/xianyu/ {
        proxy_pass http://127.0.0.1:8766;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 15s;
        client_max_body_size 16m;
    }

'''
location_pattern = re.compile(r'(?ms)^[ \t]*location[ \t]+\^~[ \t]+/api/xianyu/[ \t]*\{\n.*?^[ \t]*\}[ \t]*\n?')
match = location_pattern.search(old)
if match:
    current_block = match.group(0)
    if 'proxy_pass http://127.0.0.1:8766;' not in current_block:
        raise RuntimeError('Unexpected existing xianyu nginx location')
    updated_block = current_block
    directives = (
        (r'(?m)^[ \t]*proxy_set_header\s+X-Forwarded-For\s+[^;]+;', '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;'),
        (r'(?m)^[ \t]*proxy_set_header\s+X-Real-IP\s+[^;]+;', '        proxy_set_header X-Real-IP $remote_addr;'),
        (r'(?m)^[ \t]*client_max_body_size\s+[^;]+;', '        client_max_body_size 16m;'),
    )
    for pattern, line in directives:
        if re.search(pattern, updated_block):
            updated_block = re.sub(pattern, line, updated_block, count=1)
        else:
            updated_block = updated_block.replace('\n    }', '\n' + line + '\n    }', 1)
    updated = old[:match.start()] + updated_block + old[match.end():]
else:
    if old.count(marker) != 1:
        raise RuntimeError('Unexpected nginx configuration')
    updated = old.replace(marker, block + marker)
if updated != old:
    backup = str(nginx) + '.bak-xianyu-' + stamp
    shutil.copy2(nginx, backup)
    nginx.write_text(updated)
    if subprocess.run(['nginx', '-t']).returncode:
        shutil.copy2(backup, nginx)
        raise RuntimeError('Invalid nginx config; restored backup')
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
print('Xianyu auth installed on loopback port 8766')
