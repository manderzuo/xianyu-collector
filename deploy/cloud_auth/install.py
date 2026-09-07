"""Run with sudo on the existing Gemstory host, after uploading this directory."""
import os
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

root = Path('/opt/gemstory/xianyu-auth')
data = Path('/var/lib/gemstory/xianyu-auth')
nginx = Path('/etc/nginx/sites-available/filmcrew.conf')
stamp = datetime.now().strftime('%Y%m%d%H%M%S')
subprocess.run(['id', 'xianyu-auth'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 or subprocess.run(['useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', 'xianyu-auth'], check=True)
root.mkdir(parents=True, exist_ok=True)
data.mkdir(parents=True, exist_ok=True)
session_key = data / 'session.key'
if not session_key.exists():
    session_key.write_text(secrets.token_urlsafe(48) + '\n')
    session_key.chmod(0o600)
session_env = data / 'session.env'
session_env.write_text('XIANYU_CLOUD_SESSION_KEY=' + session_key.read_text().strip() + '\n')
session_env.chmod(0o600)
db = data / 'server.db'
if db.exists():
    with sqlite3.connect(db) as source, sqlite3.connect(data / ('backup-' + stamp + '.db')) as target:
        source.backup(target)
for name in ['server.py', 'auth_store.py', 'admin.html']:
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
    with sqlite3.connect('file:/var/lib/gemstory/collector-auth/server.db?mode=ro', uri=True) as source:
        row = source.execute("SELECT password_hash FROM app_users WHERE username='admin' AND role='admin'").fetchone()
    if not row:
        raise RuntimeError('Existing administrator not found')
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE app_users SET password_hash=? WHERE username='admin'", (row[0],))
shutil.chown(data, 'xianyu-auth', 'xianyu-auth')
for p in data.iterdir():
    shutil.chown(p, 'xianyu-auth', 'xianyu-auth')
    p.chmod(0o600)
data.chmod(0o700)
unit = Path('/etc/systemd/system/xianyu-auth.service')
unit.write_text('''[Unit]
Description=Xianyu registration approval API
After=network-online.target
[Service]
User=xianyu-auth
Group=xianyu-auth
WorkingDirectory=/opt/gemstory/xianyu-auth
EnvironmentFile=/var/lib/gemstory/xianyu-auth/session.env
ExecStart=/usr/bin/python3 /opt/gemstory/xianyu-auth/server.py --db /var/lib/gemstory/xianyu-auth/server.db --port 8766
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/var/lib/gemstory/xianyu-auth
[Install]
WantedBy=multi-user.target
''')
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'xianyu-auth'], check=True)
subprocess.run(['systemctl', 'restart', 'xianyu-auth'], check=True)
old = nginx.read_text()
if 'location ^~ /api/xianyu/' not in old:
    backup = str(nginx) + '.bak-xianyu-' + stamp
    shutil.copy2(nginx, backup)
    marker = '    location ^~ /api/collector/'
    if old.count(marker) != 1:
        raise RuntimeError('Unexpected nginx configuration')
    block = '''    location ^~ /api/xianyu/ {
        proxy_pass http://127.0.0.1:8766;
        proxy_set_header Host $host;
        proxy_read_timeout 15s;
        client_max_body_size 256k;
    }

'''
    nginx.write_text(old.replace(marker, block + marker))
    if subprocess.run(['nginx', '-t']).returncode:
        shutil.copy2(backup, nginx)
        raise RuntimeError('Invalid nginx config; restored backup')
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
print('Xianyu auth installed on loopback port 8766')
