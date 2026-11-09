"""Session renewal reuses the existing outboxes without replaying old deliveries."""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4
from zoneinfo import ZoneInfo

from briefing import Briefings
from inputs.wechat import receive_commands
from memory.store import TaskStore
from personal_context.wechat_push import PushError, SESSION_EXPIRED, SESSION_UNAVAILABLE
from presentation.push import PushDelivery


def at(hour, minute=0, day=8):
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class WeChatSessionTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / 'assistant.db')
        self.wechat = SimpleNamespace(bot_id='bot', recipient_id='self', state=lambda: {},
                                      poll=Mock(), send=Mock(return_value='accepted'))
        self.store.wechat_state(self.wechat, now=at(7))
        self.briefings = Briefings(self.store, wechat=self.wechat)
        self.push = PushDelivery(self.store, wechat=self.wechat, briefings=self.briefings)

    def message(self, now, **values):
        return dict(dict(message_id='message-' + str(now), message_type=1, from_user_id='self',
                         create_time_ms=int(now * 1000), context_token='session'), **values)

    def receive(self, *messages, now=None):
        self.wechat.poll.return_value = dict(msgs=list(messages), get_updates_buf='next')
        receive_commands(self.store, self.wechat, now or at(10, 5))

    def row(self, table, item_id):
        key = 'reminder_id' if table == 'wechat_deliveries' else 'id'
        with self.store.connect() as db:
            return dict(db.execute(f'SELECT * FROM {table} WHERE {key}=?', (item_id,)).fetchone())

    def seed(self, queue, **values):
        kind = queue
        uid = uuid4().hex
        if kind == 'task':
            task = self.store.create(description='提交材料', now=at(8), wake_at=at(20))
            item_id = self.store.commit_check(task, observed=None, now=at(8),
                reminder=dict(title='提交材料', body='请处理'), context={}, wechat=self.wechat)
            table = 'wechat_deliveries'
        elif kind == 'event':
            event = self.store.create_event(title='会面', starts_at=at(10, 20), ends_at=at(11))
            self.store.queue_event_reminders(self.wechat, at(10))
            table = 'event_reminders'
            with self.store.connect() as db:
                item_id = db.execute('SELECT id FROM event_reminders WHERE event_id=?', (event['id'],)).fetchone()[0]
        elif kind == 'insight':
            table = 'insights'
            with self.store.connect() as db:
                item_id = db.execute('INSERT INTO insights (source_key,anchor_id,title,body,reason,'
                    'profile_ids_json,evidence_json,created_at,notification_status,client_id,recipient_id,bot_id,'
                    'expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('source', uid, '附近活动', '内容', '理由', '[]', '[]', at(8), 'pending', uid,
                     'self', 'bot', at(20))).lastrowid
        else:
            table = 'briefing_deliveries'
            with self.store.connect() as db:
                item_id = db.execute('INSERT INTO briefing_deliveries '
                    '(kind,day,title,body,items_json,client_id,created_at,recipient_id,bot_id) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    ('changes', '2026-11-08', '安排变化', '内容', '{}', uid, at(8), 'self', 'bot')).lastrowid
        status = 'notification_status' if kind == 'insight' else 'status'
        attempts = 'notification_attempts' if kind == 'insight' else 'attempts'
        error = 'notification_error' if kind == 'insight' else 'error'
        fields = {status: 'failed', attempts: 3, error: SESSION_EXPIRED, **values}
        key = 'reminder_id' if kind == 'task' else 'id'
        with self.store.connect() as db:
            db.execute(f'UPDATE {table} SET ' + ','.join(f'{k}=?' for k in fields) + f' WHERE {key}=?',
                       (*fields.values(), item_id))
        return table, item_id

    def test_same_token_new_message_recovers_and_daily_is_delivered(self):
        # A token may stay the same when a new user message renews its validity.
        self.receive(self.message(at(8)), now=at(8))
        self.briefings.sync(at(10))
        self.wechat.send.side_effect = PushError(SESSION_EXPIRED, status='retry')
        for minute in (0, 1, 2):
            self.push.send_briefing(at(10, minute))
        with self.store.connect() as db:
            item_id = db.execute('SELECT id FROM briefing_deliveries').fetchone()[0]
        self.assertEqual(self.row('briefing_deliveries', item_id)['status'], 'failed')
        self.receive(self.message(at(10, 5)))
        recovered = self.row('briefing_deliveries', item_id)
        self.assertEqual((recovered['status'], recovered['attempts'], recovered['error']), ('pending', 0, None))
        self.assertEqual(self.wechat.context, 'session')
        self.wechat.send.side_effect = None
        self.push.send_briefing(at(10, 5))
        self.assertEqual(self.row('briefing_deliveries', item_id)['status'], 'delivered')
        self.push.send_briefing(at(10, 6))
        self.assertEqual(self.wechat.send.call_count, 4)

    def test_all_effective_queues_resume_and_duplicates_do_not_reset_attempts(self):
        queues = [self.seed(kind) for kind in ('task', 'event', 'insight', 'briefing')]
        message = self.message(at(10, 5))
        self.receive(message)
        for table, item_id in queues:
            row = self.row(table, item_id)
            prefix = 'notification_' if table == 'insights' else ''
            self.assertEqual((row[prefix + 'status'], row[prefix + 'attempts'], row[prefix + 'error']),
                             ('pending', 0, None))
        task_id = queues[0][1]
        self.store.claim_wechat(at(10, 5))
        self.store.wechat_result(task_id, status='retry', error=SESSION_EXPIRED, now=at(10, 5))
        self.receive(message)
        self.receive(self.message(at(8), context_token=None))
        self.assertEqual(self.row('wechat_deliveries', task_id)['attempts'], 1)
        self.assertEqual(self.row('wechat_deliveries', task_id)['status'], 'retry')

    def test_old_other_and_contextless_messages_do_not_recover(self):
        table, item_id = self.seed('briefing')
        original = self.message(at(10))
        variants = [dict(original, from_user_id='other'), dict(original, message_type=2),
                    dict(original, message_id=None), dict(original, create_time_ms=int(at(6)*1000)),
                    dict(original, context_token=None)]
        for message in variants:
            self.receive(message)
            self.assertEqual(self.row(table, item_id)['status'], 'failed')
        self.receive(original)
        self.assertEqual(self.row(table, item_id)['status'], 'pending')
        self.receive(dict(original, context_token='stale', create_time_ms=int(at(9)*1000)))
        self.assertEqual(self.wechat.context, 'session')

    def test_ineligible_states_errors_and_accounts_stay_untouched(self):
        skipped = []
        for status in ('delivered', 'unknown', 'superseded', 'dismissed', 'expired', 'disabled'):
            skipped.append(self.seed('briefing', status=status))
        skipped.extend([self.seed('briefing', error='微信连接失败'),
                        self.seed('briefing', recipient_id='other'),
                        self.seed('briefing', bot_id='other'),
                        self.seed('briefing', kind='daily', day='2026-11-07')])
        task = self.seed('task')
        read = self.seed('task')
        deleted = self.seed('task')
        event = self.seed('event')
        ended = self.seed('event', starts_at=at(9))
        skipped.extend([task, read, deleted, event, ended,
                        self.seed('insight', read_at=at(9)),
                        self.seed('insight', expires_at=at(10))])
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET status='done' WHERE id=(SELECT task_id FROM reminders WHERE id=?)", (task[1],))
            db.execute('UPDATE reminders SET read_at=? WHERE id=?', (at(9), read[1]))
            db.execute('UPDATE tasks SET deleted_at=? WHERE id=(SELECT task_id FROM reminders WHERE id=?)',
                       (at(9), deleted[1]))
            db.execute("UPDATE calendar_events SET status='cancelled' WHERE id=(SELECT event_id FROM event_reminders WHERE id=?)", (event[1],))
        before = [self.row(*item) for item in skipped]
        valid = self.seed('briefing', status='retry', error=SESSION_UNAVAILABLE)
        self.receive(self.message(at(10, 5)))
        self.assertEqual([self.row(*item) for item in skipped], before)
        self.assertEqual(self.row(*valid)['status'], 'pending')

    def test_recovery_preserves_quiet_hours_and_latest_daily_contents(self):
        self.briefings.sync(at(8))
        with self.store.connect() as db:
            item_id = db.execute('SELECT id FROM briefing_deliveries').fetchone()[0]
            db.execute("UPDATE briefing_deliveries SET status='failed',attempts=3,error=?,recipient_id='self',bot_id='bot'",
                       (SESSION_EXPIRED,))
        self.store.create(description='新待办', now=at(9), due_at=at(18))
        self.receive(self.message(at(9)), now=at(9))
        self.briefings.sync(at(9))
        self.push.send_briefing(at(9))
        self.wechat.send.assert_not_called()
        self.assertEqual(self.row('briefing_deliveries', item_id)['status'], 'pending')
        self.push.send_briefing(at(10))
        self.wechat.send.assert_called_once()
        self.assertIn('新待办', self.wechat.send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
