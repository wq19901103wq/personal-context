"""Text delivery through the existing ClawBot/iLink login, without copying secrets."""

import base64
import json
import os
from pathlib import Path
import secrets
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


SESSION_UNAVAILABLE = '微信会话不可用，请给 ClawBot 发一条消息后重试'
SESSION_EXPIRED = '微信会话已过期，请给 ClawBot 发一条消息后重试'
SESSION_ERRORS = (SESSION_UNAVAILABLE, SESSION_EXPIRED)


class PushError(RuntimeError):
    def __init__(self, message, *, status='failed'):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WeChatPush:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('WECHAT_CREDENTIALS_DIR')
                              or '~/.mcp-wechat-server').expanduser()
        # Match the existing watchdog: iLink is reached directly, independent of the local proxy.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        account = self.account()
        self.recipient_id, self.bot_id = account['userId'], account['botId']
        self.context = None
        self.saved_context = None

    def state(self):
        try:
            return json.loads((self.directory / 'state.json').read_text())
        except (OSError, ValueError):
            return {}

    def headers(self, account):
        return {'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
                'Authorization': 'Bearer ' + account['botToken'],
                'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()}

    def poll(self, cursor):
        account = self.account()
        if (account['userId'], account['botId']) != (self.recipient_id, self.bot_id):
            raise PushError('ClawBot 绑定已改变，请重启助理以使用新账号')
        request = Request('https://ilinkai.weixin.qq.com/ilink/bot/getupdates',
                          data=json.dumps(dict(get_updates_buf=cursor,
                                              base_info=dict(channel_version='1.0.0'))).encode(),
                          headers=self.headers(account), method='POST')
        try:
            with self.opener.open(request, timeout=5) as response:
                result = json.loads(response.read())
        except (TimeoutError, socket.timeout):
            return dict(msgs=[], get_updates_buf=cursor)
        except (OSError, ValueError):
            raise PushError('ClawBot 暂时无法收取消息，待办提醒仍会继续', status='retry') from None
        if not isinstance(result, dict) or any(result.get(key, 0) != 0 for key in ('ret', 'errcode')):
            raise PushError('ClawBot 收信接口返回失败，稍后重试', status='retry')
        return result

    def account(self):
        try:
            account = json.loads((self.directory / 'account.json').read_text())
            if not all(account.get(k) for k in ('botToken', 'botId', 'userId')):
                raise ValueError
        except (OSError, ValueError):
            raise PushError('ClawBot 登录不可用，请恢复已有微信登录', status='retry') from None
        base = account.get('baseUrl') or 'https://ilinkai.weixin.qq.com'
        if urlparse(base)._replace(path='', params='', query='', fragment='').geturl() != 'https://ilinkai.weixin.qq.com':
            raise PushError('ClawBot 的微信 API 地址不受支持')
        return account

    def refresh_context(self, request, state):
        # One lookup when needed. Do not advance ClawBot's cursor or save incoming messages.
        lookup = Request('https://ilinkai.weixin.qq.com/ilink/bot/getupdates',
                         data=json.dumps(dict(get_updates_buf=state.get('updatesBuf', ''),
                                              base_info=dict(channel_version='1.0.0'))).encode(),
                         headers={key: value for key, value in request.header_items()
                                  if key.lower() != 'content-length'}, method='POST')
        try:
            with self.opener.open(lookup, timeout=5) as response:
                result = json.loads(response.read())
            for msg in reversed(result.get('msgs') or []):
                if msg.get('from_user_id') == self.recipient_id and msg.get('context_token'):
                    self.context = msg['context_token']
                    return self.context
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def send(self, text, *, client_id, recipient_id, bot_id, _refreshed=False):
        account = self.account()
        # A queued reminder keeps its original self-chat destination across login changes.
        if (account['userId'], account['botId']) != (recipient_id, bot_id):
            raise PushError('ClawBot 绑定已改变，这条旧提醒未发送')
        try:
            state = json.loads((self.directory / 'state.json').read_text())
            stored = state.get('contextTokens', {}).get(f'{bot_id}:{recipient_id}')
            if stored != self.saved_context:
                self.context = self.saved_context = stored
            context = self.context
        except (OSError, ValueError):
            state = {}
            context = self.context
        request = Request('https://ilinkai.weixin.qq.com/ilink/bot/sendmessage',
                          data=json.dumps(dict(msg=dict(
                              from_user_id='', to_user_id=recipient_id, client_id=client_id,
                              message_type=2, message_state=2, context_token=context,
                              item_list=[dict(type=1, text_item=dict(text=text))]),
                              base_info=dict(channel_version='1.0.0'))).encode(),
                          headers=self.headers(account),
                          method='POST')
        if not context:
            if not _refreshed and self.refresh_context(request, state):
                return self.send(text, client_id=client_id, recipient_id=recipient_id,
                                 bot_id=bot_id, _refreshed=True)
            raise PushError(SESSION_UNAVAILABLE, status='retry')
        try:
            with self.opener.open(request, timeout=15) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            status = 'retry' if exc.code == 429 else ('unknown' if exc.code >= 500 else 'failed')
            raise PushError(f'微信推送 HTTP {exc.code}', status=status) from None
        except URLError as exc:
            before_send = isinstance(exc.reason, (socket.gaierror, ConnectionRefusedError))
            raise PushError('微信连接失败' if before_send else '微信投递结果未知，请查看微信',
                            status='retry' if before_send else 'unknown') from None
        except (OSError, ValueError):
            raise PushError('微信投递结果未知，请查看微信', status='unknown') from None
        if not isinstance(result, dict):
            raise PushError('微信未返回投递确认，请查看微信', status='unknown')
        codes = {key: result[key] for key in ('ret', 'errcode') if key in result}
        if any(value != 0 for value in codes.values()):
            if -2 in codes.values():
                fresh = self.refresh_context(request, state) if not _refreshed else None
                if fresh and fresh != context:
                    return self.send(text, client_id=client_id, recipient_id=recipient_id,
                                     bot_id=bot_id, _refreshed=True)
                raise PushError(SESSION_EXPIRED, status='retry')
            safe_codes = {key: value for key, value in codes.items() if isinstance(value, int)}
            raise PushError(f'微信未接受推送：{safe_codes}')
        if not codes and not result.get('message_id'):
            raise PushError('微信未返回投递确认，请查看微信', status='unknown')
        return str(result.get('message_id') or client_id)
