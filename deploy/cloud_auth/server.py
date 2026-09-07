"""Xianyu authentication API; business data stays on customer computers."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from auth_store import AuthError, AuthStore


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip('/') == '/api/xianyu/auth/health':
            return self.reply(200, {'ok': True, 'service': 'xianyu-auth'})
        if self.path.rstrip('/') == '/api/xianyu/auth/admin':
            raw = Path(__file__).with_name('admin.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.reply(404, {'ok': False, 'message': '接口不存在'})

    def do_POST(self):
        if not self.path.startswith('/api/xianyu/auth/'):
            return self.reply(404, {'ok': False, 'message': '接口不存在'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                return self.reply(413, {'ok': False, 'message': '请求大小无效'})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
            action = self.path.rstrip('/').rsplit('/', 1)[-1]
            store = self.server.store
            if action == 'register':
                user = store.register(body.get('username'), body.get('password'), body.get('nickname') or body.get('username'), body.get('invite_code'))
                return self.reply(200, {'ok': True, 'user': user, 'message': '注册申请已提交，请等待管理员审核'})
            if action == 'login':
                user = store.authenticate(body.get('username'), body.get('password'))
                return self.reply(200, {'ok': True, 'user': user, 'session_token': store.create_session(user['id'])})
            header = self.headers.get('Authorization', '')
            token = header[7:] if header.startswith('Bearer ') else ''
            user = store.get_session_user(token)
            if not user:
                return self.reply(401, {'ok': False, 'message': '登录已失效或账号未获批准，请重新登录'})
            if action == 'me':
                return self.reply(200, {'ok': True, 'user': user})
            if action == 'logout':
                store.revoke_session(token)
                return self.reply(200, {'ok': True})
            if user['role'] != 'admin':
                return self.reply(403, {'ok': False, 'message': '只有管理员可以审批'})
            if action == 'list_users':
                return self.reply(200, {'ok': True, 'items': store.list_users()})
            if action == 'sync_invites':
                return self.reply(200, {'ok': True, 'items': store.sync_invites(body.get('items') or [])})
            operations = {'approve_user': store.approve, 'reject_user': store.reject, 'disable_user': store.disable}
            if action in operations:
                target = int(body.get('user_id', 0))
                return self.reply(200, {'ok': True, 'user': operations[action](target)})
            return self.reply(404, {'ok': False, 'message': '接口不存在'})
        except AuthError as exc:
            self.reply(400 if exc.code == 'invalid_input' else 403, {'ok': False, 'code': exc.code, 'message': exc.message})
        except (ValueError, TypeError):
            self.reply(400, {'ok': False, 'message': '请求格式无效'})
        except Exception:
            self.reply(500, {'ok': False, 'message': '认证服务暂不可用'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.store = AuthStore(args.db)
    server.serve_forever()
