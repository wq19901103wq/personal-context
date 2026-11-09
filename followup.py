"""Application scheduling joins inputs, memory and result delivery."""

from dataclasses import asdict
from datetime import datetime
from functools import partial
import logging
import threading
import time
from zoneinfo import ZoneInfo

from personal_context.wechat_push import PushError
from inputs.wechat import SOURCE_ERRORS, history_reader, recent_messages, message_ids, receive_commands
from memory.context import knowledge_context
from presentation.push import PushDelivery, desktop_notification
from briefing import Briefings
from inputs.apple_calendar import AppleCalendar

log = logging.getLogger(__name__)


class FollowUp(PushDelivery):
    def __init__(self, store, *, notify=False, notifier=desktop_notification, wechat=None, public_url=None):
        super().__init__(store, wechat=wechat, public_url=public_url,
                         briefings=Briefings(store, wechat=wechat), notifier=notifier)
        self.notify = notify
        self.calendar = AppleCalendar(store)
        self.wechat_error = None
        self.lock = threading.Lock()
        self.last_tick = None
        self.last_error = None
        self.last_delivery = None
        self.delivery_error = None

    def tick(self, *, now=None):
        # One-shot CLI/API use still does both, even if a source check fails.
        try:
            return self.check(now=now)
        finally:
            self.deliver(now=now)

    def check(self, *, now=None):
        now = time.time() if now is None else now
        with self.lock, self.store.worker_lock():
            self.calendar.sync(now=now)
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
            self.last_tick, self.last_error = now, None
            return reminders

    def deliver(self, *, now=None):
        with self.store.delivery_lock() as acquired:
            if not acquired:
                return False
            actions = []
            if self.wechat:
                self.store.recover_wechat()
                actions.extend([
                    ('微信收信', self.receive_wechat),
                    ('日程提醒', partial(self.send_event_reminders, limit=1)),
                    ('待办提醒', partial(self.send_reminders, limit=1)),
                    ('指令回执', partial(self.send_replies, limit=1)),
                ])
            actions.append(('简报', self.send_briefing))
            if self.wechat:
                actions.append(('推荐', self.send_insight))
            if self.notify:
                actions.append(('本机通知', self.send_notifications))
            errors = []
            for name, action in actions:
                try:
                    # Don't use a timestamp from before a slow read or network request.
                    action(time.time() if now is None else now)
                except Exception as exc:
                    errors.append(name + '：' + str(exc))
                    log.exception('%s本轮失败，其他推送继续', name)
            self.last_delivery = time.time() if now is None else now
            self.delivery_error = '\n'.join(errors) or None
            return True

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

    def run_checks(self, stop, *, interval):
        while not stop.is_set():
            try:
                self.check()
            except Exception as exc:
                # Keep future reminders alive and make an unexpected tick failure visible.
                self.last_error = str(exc)
                log.exception('本轮跟进失败')
            stop.wait(interval)

    def run(self, stop, *, interval=30):
        checker = threading.Thread(target=self.run_checks, args=(stop,),
            kwargs=dict(interval=interval), name='follow-up-checks', daemon=True)
        checker.start()
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    self.deliver()
                except Exception as exc:
                    self.delivery_error = str(exc)
                    log.exception('推送本轮失败，稍后重试')
                stop.wait(max(0, min(interval, 5) - (time.monotonic() - started)))
        finally:
            stop.set()
            checker.join(timeout=15)
