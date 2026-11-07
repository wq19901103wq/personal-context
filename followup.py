"""Application scheduling joins inputs, memory and result delivery."""

from dataclasses import asdict
from datetime import datetime
import logging
import threading
import time
from zoneinfo import ZoneInfo

from personal_context.wechat_push import PushError
from inputs.wechat import SOURCE_ERRORS, history_reader, recent_messages, message_ids, receive_commands
from memory.context import knowledge_context
from presentation.push import PushDelivery, desktop_notification
from briefing import Briefings

log = logging.getLogger(__name__)


class FollowUp(PushDelivery):
    def __init__(self, store, *, notify=False, notifier=desktop_notification, wechat=None, public_url=None):
        super().__init__(store, wechat=wechat, public_url=public_url,
                         briefings=Briefings(store, wechat=wechat), notifier=notifier)
        self.notify = notify
        self.wechat_error = None
        self.lock = threading.Lock()
        self.last_tick = None
        self.last_error = None

    def tick(self, *, now=None):
        now = time.time() if now is None else now
        with self.lock, self.store.worker_lock():
            if self.wechat:
                self.store.recover_wechat()
                self.receive_wechat(now)
            reminders, chat_cache = [], {}
            for task in self.store.tasks(active_only=True):
                scope = task['scope']
                observed, messages, errors, incoming = task['observed'], (), [], []
                if scope.get('chat_id') and scope.get('source_kind', 'wechat') == 'wechat':
                    key = (scope['history_db'], scope['history_account'],
                           scope.get('history_source'), scope['chat_id'])
                    if key not in chat_cache:
                        try:
                            chat_cache[key] = (recent_messages(scope), None)
                        except SOURCE_ERRORS as exc:
                            chat_cache[key] = ((), str(exc))
                    messages, error = chat_cache[key]
                    if error:
                        errors.append(dict(source=scope['history_db'], error=error))
                    else:
                        observed = message_ids(messages)
                        if task['observed'] is not None:
                            known = set(task['observed'])
                            incoming = [m for m in messages if m.ref.message_id not in known
                                        and m.sender.is_self is False]
                due = task['wake_at'] is not None and task['wake_at'] <= now
                if not due and not incoming:
                    if observed != task['observed']:
                        self.store.commit_check(task, observed=observed, now=now)
                    continue
                reasons = []
                if due:
                    reasons.append('deadline' if task['due_at'] is not None and task['due_at'] <= now
                                   else 'scheduled')
                if incoming:
                    reasons.append('new_message')
                knowledge = knowledge_context(scope)
                context = dict(messages=[asdict(m) for m in messages],
                               incoming_refs=[asdict(m.ref) for m in incoming],
                               knowledge=knowledge, source_errors=errors + knowledge['errors'],
                               observed_at=now, coverage='recent_30_only',
                               task_description=task['description'], task_notes=task['notes'],
                               due_at=task['due_at'], timezone=task['timezone'])
                body = self.body(task, reasons, incoming, context)
                reminder_id = self.store.commit_check(
                    task, observed=observed, now=now, trigger='+'.join(reasons),
                    reminder=dict(title=task['description'], body=body), context=context,
                    notify=self.notify, wechat=self.wechat)
                if reminder_id is not None:
                    reminders.append(reminder_id)
            if self.notify:
                self.send_notifications(now)
            self.briefings.sync(now)
            if self.wechat:
                self.send_reminders(now)
                self.send_replies(now)
                self.send_event_reminders(now)
                self.send_insight(now)
                self.send_briefing(now)
            self.last_tick, self.last_error = now, None
            return reminders

    def receive_wechat(self, now):
        try:
            receive_commands(self.store, self.wechat, now, public_url=self.public_url)
            self.wechat_error = None
        except PushError as exc:
            self.wechat_error = str(exc)
            log.warning('微信收信：%s', exc)

    @staticmethod
    def body(task, reasons, incoming, context):
        parts = []
        if 'deadline' in reasons:
            parts.append('已到约定期限，请确认进展。')
        elif 'scheduled' in reasons:
            parts.append('到了跟进时间，请处理或调整下一次跟进。')
        if incoming:
            parts.append(f'关联会话有 {len(incoming)} 条新消息，建议查看后决定下一步。')
        if task['status'] == 'waiting':
            parts.append('这项任务正在等待回复或外部进展。')
        if task['due_at'] is not None:
            stamp = datetime.fromtimestamp(task['due_at'], ZoneInfo(task['timezone']))
            parts.append('截止：' + stamp.strftime('%Y-%m-%d %H:%M %Z') + '。')
        if context['source_errors']:
            parts.append('部分来源暂时无法读取；提醒已保留，可以查看具体错误。')
        return '\n'.join(parts)

    def run(self, stop, *, interval=30):
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                # Keep future reminders alive and make an unexpected tick failure visible.
                self.last_error = str(exc)
                log.exception('本轮跟进失败')
            stop.wait(interval)
