"""Delivery scheduling and restart safety with SQLite and a fake WeChat transport."""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from followup import FollowUp
from memory.store import TaskStore
from personal_context.wechat_push import PushError


def at(hour, minute=0):
    return datetime(2026, 11, 9, hour, minute, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class PushReliabilityTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / 'assistant.db')
        self.wechat = SimpleNamespace(bot_id='bot', recipient_id='self', state=lambda: {},
            poll=Mock(return_value=dict(msgs=[], get_updates_buf='next')),
            send=Mock(return_value='accepted'))
        self.followup = FollowUp(self.store, wechat=self.wechat)

    def reminder(self):
        task = self.store.create(description='提交材料', now=at(8), wake_at=at(20))
        return self.store.commit_check(task, observed=None, now=at(8),
            reminder=dict(title='提交材料', body='请处理'), context={}, wechat=self.wechat)

    def disable_briefings(self):
        self.followup.briefings.configure(dict(daily_enabled=False, changes_enabled=False))

    def row(self, table, key, value):
        with self.store.connect() as db:
            return dict(db.execute(f'SELECT * FROM {table} WHERE {key}=?', (value,)).fetchone())

    def test_slow_calendar_does_not_block_daily_briefing(self):
        entered, release, sent, stop = Event(), Event(), Event(), Event()

        def slow_calendar(**kwargs):
            entered.set()
            release.wait(5)

        self.followup.calendar.sync = slow_calendar
        self.wechat.send.side_effect = lambda *a, **kw: sent.set() or 'accepted'
        runner = Thread(target=self.followup.run, args=(stop,), kwargs=dict(interval=.02))
        with patch('followup.time.time', return_value=at(10)):
            runner.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(sent.wait(2), 'briefing was blocked by the calendar read')
                self.assertFalse(release.is_set())
            finally:
                stop.set()
                release.set()
                runner.join(3)
        self.assertFalse(runner.is_alive())
        self.wechat.send.assert_called_once()
        self.assertIn('每日简报', self.wechat.send.call_args.args[0])
        self.assertEqual(self.followup.briefings.view(at(10))['history'][0]['status'], 'delivered')

    def test_source_failure_still_delivers_due_event_in_one_shot_tick(self):
        self.disable_briefings()
        self.store.create_event(title='会面', starts_at=at(10, 20), ends_at=at(11))
        self.followup.calendar.sync = Mock(side_effect=RuntimeError('source unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'source unavailable'):
            self.followup.tick(now=at(10))
        self.wechat.send.assert_called_once()
        self.assertIn('日程提醒', self.wechat.send.call_args.args[0])

    def test_event_priority_and_bounded_backlogs(self):
        self.disable_briefings()
        reminders = [self.reminder() for _ in range(4)]
        self.store.create_event(title='会面', starts_at=at(10, 20), ends_at=at(11))
        self.store.create_event(title='接人', starts_at=at(10, 25), ends_at=at(11))
        self.followup.deliver(now=at(10))
        texts = [call.args[0] for call in self.wechat.send.call_args_list]
        self.assertEqual(len(texts), 2)
        self.assertIn('日程提醒', texts[0])
        self.assertIn('待办', texts[1])
        self.assertEqual([self.row('wechat_deliveries', 'reminder_id', i)['status']
                          for i in reminders], ['delivered', 'pending', 'pending', 'pending'])
        self.followup.deliver(now=at(10) + 5)
        clients = [call.kwargs['client_id'] for call in self.wechat.send.call_args_list]
        self.assertEqual(len(clients), 4)
        self.assertEqual(len(set(clients)), 4)

    def test_one_broken_queue_does_not_starve_others_and_recovers_next_round(self):
        reminder = self.reminder()
        with patch.object(self.followup, 'send_event_reminders', side_effect=RuntimeError('queue unavailable')):
            with self.assertLogs('followup', level='ERROR'):
                self.followup.deliver(now=at(10))
        self.assertEqual(self.row('wechat_deliveries', 'reminder_id', reminder)['status'], 'delivered')
        self.assertEqual(self.followup.briefings.view(at(10))['history'][0]['status'], 'delivered')
        self.assertIn('日程提醒', self.followup.delivery_error)
        self.assertEqual(self.wechat.send.call_count, 2)
        self.followup.deliver(now=at(10) + 5)
        self.assertIsNone(self.followup.delivery_error)
        self.assertEqual(self.wechat.send.call_count, 2)

    def test_known_rejection_retries_only_when_due_with_same_client_id(self):
        self.disable_briefings()
        reminder = self.reminder()
        self.wechat.send.side_effect = PushError('connection failed', status='retry')
        with self.assertLogs('presentation.push', level='WARNING'):
            self.followup.deliver(now=at(10))
        self.followup.deliver(now=at(10) + 30)
        self.assertEqual(self.wechat.send.call_count, 1)
        self.wechat.send.side_effect = None
        self.followup.deliver(now=at(10) + 60)
        self.followup.deliver(now=at(10) + 65)
        self.assertEqual(self.wechat.send.call_count, 2)
        self.assertEqual(self.wechat.send.call_args_list[0].kwargs['client_id'],
                         self.wechat.send.call_args_list[1].kwargs['client_id'])
        row = self.row('wechat_deliveries', 'reminder_id', reminder)
        self.assertEqual((row['status'], row['attempts']), ('delivered', 2))

    def test_competing_sender_cannot_recover_or_repeat_an_inflight_delivery(self):
        self.disable_briefings()
        reminder = self.reminder()
        entered, release = Event(), Event()

        def slow_send(*args, **kwargs):
            entered.set()
            release.wait(5)
            return 'accepted'

        self.wechat.send.side_effect = slow_send
        runner = Thread(target=self.followup.deliver, kwargs=dict(now=at(10)))
        contender = FollowUp(TaskStore(self.store.path), wechat=self.wechat)
        runner.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(contender.deliver(now=at(10)))
            self.assertEqual(self.row('wechat_deliveries', 'reminder_id', reminder)['status'], 'sending')
        finally:
            release.set()
            runner.join(3)
        self.assertFalse(runner.is_alive())
        self.assertTrue(contender.deliver(now=at(10) + 5))
        self.wechat.send.assert_called_once()
        self.assertEqual(self.row('wechat_deliveries', 'reminder_id', reminder)['status'], 'delivered')

    def test_restart_recovers_all_interrupted_queues_without_resending(self):
        reminder = self.reminder()
        self.store.claim_wechat(at(10))
        self.followup.briefings.sync(at(10))
        briefing = self.followup.briefings.claim(at(10))
        self.store.create_event(title='会面', starts_at=at(10, 20), ends_at=at(11))
        self.store.queue_event_reminders(self.wechat, at(10))
        event = self.store.claim_event_reminder(at(10))
        with self.store.connect() as db:
            insight = db.execute('INSERT INTO insights (source_key,anchor_id,title,body,reason,'
                'profile_ids_json,evidence_json,created_at,notification_status,client_id,recipient_id,bot_id,'
                'expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('source', 'anchor', '活动', '内容', '理由', '[]', '[]', at(8), 'sending', 'insight',
                 'self', 'bot', at(20))).lastrowid
            db.execute('INSERT INTO wechat_commands (bot_id,recipient_id,message_id,command,reply,'
                'client_id,created_at,retry_at,status) VALUES (?,?,?,?,?,?,?,?,?)',
                ('bot', 'self', 'incoming', '/日程', '已处理', 'reply', at(8), at(8), 'sending'))
        restarted = FollowUp(TaskStore(self.store.path), wechat=self.wechat)
        restarted.deliver(now=at(10))
        restarted.deliver(now=at(10) + 5)
        for table, key, value, status in (
            ('wechat_deliveries', 'reminder_id', reminder, 'status'),
            ('briefing_deliveries', 'id', briefing['id'], 'status'),
            ('event_reminders', 'id', event['id'], 'status'),
            ('insights', 'id', insight, 'notification_status'),
            ('wechat_commands', 'client_id', 'reply', 'status'),
        ):
            self.assertEqual(self.row(table, key, value)[status], 'unknown', table)
        self.wechat.send.assert_not_called()

    def test_slow_receive_does_not_send_an_already_started_event(self):
        self.disable_briefings()
        self.store.create_event(title='会面', starts_at=at(10, 20), ends_at=at(11))
        clock = dict(now=at(10))
        self.followup.receive_wechat = lambda now: clock.update(now=at(10, 21))
        with patch('followup.time.time', side_effect=lambda: clock['now']):
            self.followup.deliver()
        self.wechat.send.assert_not_called()
        self.assertEqual(self.followup.last_delivery, at(10, 21))


if __name__ == '__main__':
    unittest.main()
