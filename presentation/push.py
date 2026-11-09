"""Deliver persisted results through WeChat and optional desktop notifications."""

from datetime import datetime
import json
import logging
import subprocess
from zoneinfo import ZoneInfo

from personal_context.wechat_push import PushError
from briefing import change_items

log = logging.getLogger(__name__)


def desktop_notification(title, body):
    # Arguments, including user text, never become AppleScript source code.
    script = ('on run argv\n'
              'display notification (item 2 of argv) with title (item 1 of argv)\n'
              'end run')
    result = subprocess.run(['osascript', '-e', script, title, body],
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or '本机通知调用失败')


class PushDelivery:
    def __init__(self, store, *, wechat=None, public_url=None, briefings=None,
                 notifier=desktop_notification):
        self.store, self.wechat, self.public_url = store, wechat, public_url
        self.briefings, self.notifier = briefings, notifier

    def send_notifications(self, now):
        for item in self.store.pending_notifications(now):
            error = None
            try:
                # Chat excerpts stay in the local inbox, off the lock screen.
                self.notifier('个人助理 · ' + item['title'], '有一条待办需要跟进，请查看本地助理。')
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                error = str(exc)
                log.warning('本机通知 #%s 失败：%s', item['id'], error)
            self.store.notification_result(item['id'], error=error, now=now)

    def send_reminders(self, now, *, limit=10):
        for _ in range(limit):
            item = self.store.claim_wechat(now)
            if item is None:
                break
            try:
                text = f"个人助理 · 待办 #{item['task_id']}\n{item['title']}\n\n{item['body']}"
                if self.public_url:
                    text += '\n\n详情与编辑：' + self.task_url(item['task_id'])
                text += (f"\n\n快捷回复：/完成 {item['task_id']} · /取消 {item['task_id']}"
                         f"\n改期示例：/改期 {item['task_id']} 2h")
                message_id = self.wechat.send(text, client_id=item['client_id'],
                                              recipient_id=item['recipient_id'], bot_id=item['bot_id'])
            except PushError as exc:
                self.store.wechat_result(item['reminder_id'], status=exc.status, error=str(exc), now=now)
                log.warning('微信提醒 #%s：%s', item['reminder_id'], exc)
            except Exception:
                self.store.wechat_result(item['reminder_id'], status='unknown',
                                         error='微信投递结果未知，请查看微信', now=now)
                log.warning('微信提醒 #%s 投递结果未知', item['reminder_id'])
            else:
                self.store.wechat_result(item['reminder_id'], status='delivered',
                                         message_id=message_id, now=now)

    def task_url(self, task_id):
        return self.public_url + '/task/' + self.store.task_token(task_id)

    def send_briefing(self, now):
        self.briefings.sync(now)
        if not self.wechat:
            return
        item = self.briefings.claim(now)
        if not item:
            return
        try:
            sections = json.loads(item['items_json'])
            if item['kind'] == 'daily':
                body = '\n\n'.join(s['title'] + f"（{len(s['items'])} 条）\n" +
                    ('\n'.join('• ' + i['text'] for i in s['items'][:3]) or '暂无记录') +
                    ('\n其余安排请查看完整简报。' if len(s['items']) > 3 else '') for s in sections)
            else:
                changes = change_items(sections, now)
                body = f'共 {len(changes)} 项变化\n\n' + '\n\n'.join(i['text'] for i in changes[:5])
                if len(changes) > 5:
                    body += '\n其余变化请查看完整简报。'
            text = '个人助理 · ' + item['title'] + '\n\n' + body
            if self.public_url:
                text += '\n\n查看完整简报与调整安排：' + self.public_url + '/calendar/' + self.store.calendar_token()
            self.wechat.send(text, client_id=item['client_id'],
                             recipient_id=item['recipient_id'], bot_id=item['bot_id'])
        except PushError as exc:
            self.briefings.result(item['id'], exc.status, now, str(exc))
        except Exception:
            self.briefings.result(item['id'], 'unknown', now, '投递结果未知，请查看微信；不会自动重发')
        else:
            self.briefings.result(item['id'], 'delivered', now)

    def send_event_reminders(self, now, *, limit=10):
        self.store.queue_event_reminders(self.wechat, now)
        for _ in range(limit):
            item = self.store.claim_event_reminder(now)
            if item is None:
                break
            try:
                start = datetime.fromtimestamp(item['starts_at'], ZoneInfo(item['timezone']))
                remaining = max(1, int((item['starts_at'] - now + 59) // 60))
                text = (f"个人助理 · 日程提醒\n{item['title']}\n"
                        f"{remaining} 分钟后开始\n开始：{start:%Y-%m-%d %H:%M}（{item['timezone']}）")
                if item['location']:
                    text += '\n地点：' + item['location']
                if self.public_url:
                    text += '\n\n查看与调整：' + self.public_url + '/calendar/' + self.store.calendar_token()
                message_id = self.wechat.send(text, client_id=item['client_id'],
                    recipient_id=item['recipient_id'], bot_id=item['bot_id'])
            except PushError as exc:
                self.store.event_reminder_result(item['id'], status=exc.status, error=str(exc), now=now)
            except Exception:
                self.store.event_reminder_result(item['id'], status='unknown',
                    error='日程提醒投递结果未知，请查看微信', now=now)
            else:
                self.store.event_reminder_result(item['id'], status='delivered',
                    message_id=message_id, now=now)

    def send_insight(self, now, *, requested=False):
        config = self.store.personalization()
        if not config.get('enabled'):
            return
        item = self.store.claim_insight(now, respect_schedule=not requested)
        if not item:
            return
        try:
            text = f"个人助理 · 为你发现\n{item['title']}\n\n{item['body']}\n\n为什么推荐：{item['reason']}"
            if item.get('url'):
                text += '\n\n查看详情：' + item['url']
            if self.public_url:
                text += '\n\n查看推荐与日程：' + self.public_url + '/calendar/' + self.store.calendar_token()
            self.wechat.send(text, client_id=item['client_id'],
                             recipient_id=item['recipient_id'], bot_id=item['bot_id'])
        except PushError as exc:
            self.store.insight_result(item['id'], status=exc.status, error=str(exc), now=now)
        except Exception:
            self.store.insight_result(item['id'], status='unknown', error='投递结果未知，请查看微信', now=now)
        else:
            self.store.insight_result(item['id'], status='delivered', now=now)

    def send_replies(self, now, *, limit=10):
        for _ in range(limit):
            item = self.store.claim_wechat_reply(now)
            if item is None:
                break
            try:
                reply = item['reply']
                if item['task_id'] and self.public_url:
                    reply += '\n\n详情：' + self.task_url(item['task_id'])
                self.wechat.send(reply, client_id=item['client_id'],
                                 recipient_id=item['recipient_id'], bot_id=item['bot_id'])
            except PushError as exc:
                self.store.wechat_reply_result(item['client_id'], status=exc.status, error=str(exc), now=now)
            except Exception:
                self.store.wechat_reply_result(item['client_id'], status='unknown',
                                               error='回执结果未知，指令已处理', now=now)
            else:
                self.store.wechat_reply_result(item['client_id'], status='delivered', now=now)
