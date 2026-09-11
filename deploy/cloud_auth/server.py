"""Xianyu authentication API; business data stays on customer computers."""
import argparse
import json
import logging
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from auth_store import AuthError, AuthStore
from diagnostic_store import DiagnosticStore

logger = logging.getLogger("xianyu.cloud_auth")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ACCOUNT_SESSION_ACTIONS = {
    'list_account_sessions',
    'sync_account_session',
    'get_account_session',
    'delete_account_session',
}
ACCOUNT_SESSION_SYNC_DISABLED = {
    'ok': False,
    'code': 'account_session_sync_disabled',
    'message': '闲鱼账号云端会话同步已禁用，账号 Cookie 和 Token 仅保存在本机',
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info("http request_id=%s %s", getattr(self, "request_id", "-"), format % args)

    def client_source(self):
        # The service is bound to localhost and is reached through Nginx.
        # Nginx overwrites X-Real-IP, while the last X-Forwarded-For entry is
        # the address it observed. Do not use the first, client-controlled XFF
        # entry for anonymous upload throttling.
        real_ip = str(self.headers.get('X-Real-IP', '')).strip()
        if real_ip:
            return real_ip[:128]
        forwarded = [item.strip() for item in str(self.headers.get('X-Forwarded-For', '')).split(',') if item.strip()]
        if forwarded:
            return forwarded[-1][:128]
        return str(self.client_address[0] if self.client_address else '').strip()[:128]

    def _handle_sync_users(self, store, body):
        """把采集端本机用户表里的存量账号导入云端。

        鉴权二选一，任一通过即可：
        - 管理员会话令牌（管理员在客户端登录后触发，无需额外配置）；
        - ``XIANYU_CLOUD_SYNC_SECRET`` 共享密钥（采集端后端启动时自动导入，
          此时没有用户会话可用）。

        未配置共享密钥时，只接受管理员会话——失败关闭，不会因为漏配环境变量
        而变成任何人都能向云端写入账号。
        """
        import hmac as _hmac
        import os as _os

        expected = _os.environ.get('XIANYU_CLOUD_SYNC_SECRET', '').strip()
        supplied = str(body.get('sync_secret') or '').strip()
        secret_ok = bool(expected) and bool(supplied) and _hmac.compare_digest(expected, supplied)

        if not secret_ok:
            header = self.headers.get('Authorization', '')
            token = header[7:] if header.startswith('Bearer ') else ''
            user = store.get_session_user(token) if token else None
            if not user:
                return self.reply(401, {'ok': False, 'message': '登录已失效或账号未获批准，请重新登录'})
            if user.get('role') != 'admin':
                return self.reply(403, {'ok': False, 'message': '只有管理员可以导入存量账号'})

        result = store.import_users(body.get('users') or [])
        logger.info(
            "cloud user import request_id=%s created=%s skipped=%s via_secret=%s",
            self.request_id, result.get('created'), result.get('skipped'), secret_ok,
        )
        return self.reply(200, {'ok': True, **result, 'message': '存量账号已导入'})

    def reply(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        request_id = getattr(self, 'request_id', '')
        if request_id:
            self.send_header('X-Request-ID', request_id)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def reply_bytes(self, status, data, filename):
        safe_filename = Path(str(filename or 'diagnostic-report.zip')).name.replace('\r', '').replace('\n', '').replace('"', '')
        self.send_response(status)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + safe_filename)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def auth_error_status(code):
        return {
            'invalid_input': 400,
            'invalid_invite': 400,
            'username_exists': 409,
            'invalid_credentials': 401,
            'account_pending': 403,
            'account_rejected': 403,
            'account_disabled': 403,
            'not_found': 404,
            'rate_limited': 429,
            'insecure_configuration': 503,
            'connection_failed': 503,
            'not_configured': 503,
            'invalid_response': 502,
            'cloud_auth_error': 502,
            'storage_full': 503,
            'configuration_error': 503,
            'server_error': 500,
        }.get(str(code or ''), 403)

    def do_GET(self):
        self.request_id = secrets.token_hex(8)
        if urlsplit(self.path).path.rstrip('/') == '/api/xianyu/auth/health':
            return self.reply(200, {'ok': True, 'service': 'xianyu-auth'})
        if urlsplit(self.path).path.rstrip('/') == '/api/xianyu/auth/admin':
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
        path = urlsplit(self.path).path
        self.request_id = secrets.token_hex(8)
        if not path.startswith('/api/xianyu/auth/'):
            return self.reply(404, {'ok': False, 'message': '接口不存在'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16 * 1024 * 1024:
                return self.reply(413, {'ok': False, 'message': '请求大小无效'})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
            action = path.rstrip('/').rsplit('/', 1)[-1]
            store = self.server.store
            diagnostics = self.server.diagnostics
            if action == 'diagnostics_upload':
                header = self.headers.get('Authorization', '')
                token = header[7:] if header.startswith('Bearer ') else ''
                owner = store.get_session_user(token) if token else None
                if token and not owner:
                    return self.reply(401, {'ok': False, 'message': '登录已失效，请重新登录'})
                report = diagnostics.save_upload(body, owner, self.client_source())
                return self.reply(200, {'ok': True, 'report': report, 'message': '诊断日志已上传'})
            if action == 'register':
                user = store.register(body.get('username'), body.get('password'), body.get('nickname') or body.get('username'), body.get('invite_code'), self.client_source(), body.get('email'))
                return self.reply(200, {'ok': True, 'user': user, 'message': '注册申请已提交，请等待管理员审核'})
            if action == 'login':
                user = store.authenticate(body.get('username'), body.get('password'), self.client_source())
                return self.reply(200, {'ok': True, 'user': user, 'session_token': store.create_session(user['id'])})
            if action == 'sync_users':
                # 存量账号导入必须在会话校验之前处理：调用方可能是没有用户会话的
                # 采集端后端（服务器到服务器），它只能用共享密钥证明身份。
                return self._handle_sync_users(store, body)
            header = self.headers.get('Authorization', '')
            token = header[7:] if header.startswith('Bearer ') else ''
            user = store.get_session_user(token)
            if not user:
                return self.reply(401, {'ok': False, 'message': '登录已失效或账号未获批准，请重新登录'})
            if action in ACCOUNT_SESSION_ACTIONS:
                # The shared service is only the authority for application
                # login and entitlements.  Xianyu account cookies/tokens are
                # deliberately kept on the customer machine and must never be
                # uploaded or returned by this service.
                return self.reply(410, ACCOUNT_SESSION_SYNC_DISABLED)
            if action == 'me':
                return self.reply(200, {'ok': True, 'user': user})
            if action == 'logout':
                store.revoke_session(token)
                return self.reply(200, {'ok': True})
            if action == 'get_entitlements':
                requested = int(body.get('user_id') or user['id'])
                if requested != user['id'] and user['role'] != 'admin':
                    return self.reply(403, {'ok': False, 'message': '只有管理员可以查看其他账号权限'})
                return self.reply(200, {'ok': True, **store.get_entitlements(requested)})
            if action == 'list_account_sessions':
                return self.reply(200, {'ok': True, 'items': store.list_account_sessions(user['id'])})
            if action == 'sync_account_session':
                return self.reply(200, {'ok': True, 'session': store.save_account_session(user['id'], body)})
            if action == 'get_account_session':
                return self.reply(200, {'ok': True, 'session': store.get_account_session(user['id'], int(body.get('session_id') or 0))})
            if action == 'delete_account_session':
                store.delete_account_session(user['id'], int(body.get('session_id') or 0))
                return self.reply(200, {'ok': True, 'deleted': True})
            if user['role'] != 'admin':
                return self.reply(403, {'ok': False, 'message': '只有管理员可以审批'})
            if action == 'diagnostics_list':
                result = diagnostics.list_reports(status=body.get('status') or '', severity=body.get('severity') or '', search=body.get('search') or '', limit=body.get('limit') or 50, offset=body.get('offset') or 0)
                return self.reply(200, {'ok': True, **result})
            if action == 'diagnostics_stats':
                return self.reply(200, {'ok': True, 'stats': diagnostics.stats()})
            if action == 'diagnostics_detail':
                return self.reply(200, {'ok': True, 'report': diagnostics.get_public(int(body.get('report_id') or 0))})
            if action == 'diagnostics_download':
                data, filename, _ = diagnostics.download(int(body.get('report_id') or 0))
                return self.reply_bytes(200, data, filename)
            if action == 'diagnostics_status':
                report = diagnostics.set_status(int(body.get('report_id') or 0), body.get('status'), user['id'], body.get('note') or '')
                return self.reply(200, {'ok': True, 'report': report})
            if action == 'diagnostics_delete':
                diagnostics.delete(int(body.get('report_id') or 0), user['id'])
                return self.reply(200, {'ok': True, 'deleted': True})
            if action == 'list_users':
                return self.reply(200, {'ok': True, 'items': store.list_users()})
            if action == 'sync_invites':
                return self.reply(200, {'ok': True, 'items': store.sync_invites(body.get('items') or [])})
            operations = {'approve_user': store.approve, 'reject_user': store.reject, 'disable_user': store.disable}
            if action in operations:
                target = int(body.get('user_id', 0))
                return self.reply(200, {'ok': True, 'user': operations[action](target)})
            if action in {'set_user_plan', 'set_user_feature', 'delete_user_feature'}:
                target = int(body.get('user_id') or 0)
                feature_key = body.get('feature_key')
                result = store.update_entitlements(
                    target,
                    plan_code=body.get('plan_code') if action == 'set_user_plan' else None,
                    plan_expires_at=body.get('plan_expires_at') if action == 'set_user_plan' else None,
                    clear_plan_expires_at=action == 'set_user_plan' and 'plan_expires_at' in body,
                    feature_key=str(feature_key) if feature_key else None,
                    feature=body.get('feature') or {},
                    delete_feature=action == 'delete_user_feature',
                )
                return self.reply(200, {'ok': True, 'user': store.get_user(target), **result})
            return self.reply(404, {'ok': False, 'message': '接口不存在'})
        except AuthError as exc:
            logger.info("auth request rejected request_id=%s path=%s code=%s", self.request_id, path, exc.code)
            self.reply(self.auth_error_status(exc.code), {'ok': False, 'code': exc.code, 'message': exc.message})
        except (ValueError, TypeError):
            logger.info("auth request invalid request_id=%s path=%s", self.request_id, path)
            self.reply(400, {'ok': False, 'message': '请求格式无效'})
        except Exception:
            logger.exception("auth request failed request_id=%s path=%s", self.request_id, path)
            self.reply(500, {'ok': False, 'message': '认证服务暂不可用'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.store = AuthStore(args.db)
    server.diagnostics = DiagnosticStore(args.db)
    server.serve_forever()
