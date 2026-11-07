"""Briefing contents and durable delivery; never contacts a live messaging service."""

from datetime import datetime
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from personal_assistant.briefing import Briefings, bounds
from personal_assistant.followup import FollowUp
from personal_assistant.store import TaskStore
from personal_assistant.task_web import calendar_view, task_server
from personal_context.wechat_push import PushError


def at(hour, minute=0, day=7):
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class BriefingTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / 'assistant.db')
        self.wechat = SimpleNamespace(recipient_id='self', bot_id='bot', send=Mock(return_value='message'))
        self.briefings = Briefings(self.store, wechat=self.wechat)

    def task(self, **values):
        return self.store.create(description='提交材料', now=at(8), wake_at=at(20), **values)

    def event(self, **values):
        return self.store.create_event(title='会面', **values)

    def row(self, item_id):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM briefing_deliveries WHERE id=?', (item_id,)).fetchone())

    def changes_only(self):
        self.briefings.configure(dict(daily_enabled=False))

    def claim(self, now):
        with self.store.worker_lock():
            return self.briefings.claim(now)

    def test_daily_uses_recurrence_and_preserves_unknown_information(self):
        self.event(starts_at=at(14, day=6), ends_at=at(15, day=6), location='美术馆',
                   recurrence=dict(frequency='daily'))
        self.event(scheduled_date='2026-11-07', time_period='evening', status='tentative')
        self.event()
        self.event(starts_at=at(12, day=8), ends_at=at(13, day=8))
        self.event(starts_at=at(12), ends_at=at(13), status='cancelled')
        self.task(due_at=at(7))
        self.task()
        closed = self.task(due_at=at(9))
        self.store.update(closed['id'], dict(status='done'))
        self.task(due_at=at(9, day=9))
        waiting = self.task(due_at=at(9, day=9))
        self.store.update(waiting['id'], dict(status='waiting'))
        daily = self.briefings.view(at(10))['today']
        events, tasks, unknown = daily['sections']
        self.assertEqual(len(events['items']), 2)
        self.assertIn('11-07 14:00', events['items'][1]['text'])
        self.assertIn('晚上（具体时间待确认）', events['items'][0]['text'])
        self.assertIn('地点待确认', events['items'][0]['text'])
        self.assertEqual(len(tasks['items']), 3)
        self.assertIn('已逾期', tasks['items'][0]['text'])
        self.assertIn('未设截止时间', tasks['items'][-1]['text'])
        self.assertEqual(len(unknown['items']), 1)
        self.assertIn('未计入今天', unknown['title'])

    def test_uncertain_weekdays_are_labelled_and_local_days_handle_dst(self):
        self.event(starts_at=at(14, day=6), ends_at=at(15, day=6),
                   recurrence=dict(frequency='weekly', weekdays=[4], weekly_count=6))
        text = self.briefings.view(at(10))['today']['body']
        self.assertIn('今天是否安排待确认', text)
        self.assertIn('部分日期待确认', text)
        now = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo('America/New_York')).timestamp()
        day, start, end = bounds(now, 'America/New_York')
        self.assertEqual(day, '2026-11-01')
        self.assertEqual(end - start, 25 * 3600)
        self.assertEqual(bounds(at(23, 59), 'Asia/Shanghai')[0], '2026-11-07')
        self.assertEqual(bounds(at(0, day=8), 'Asia/Shanghai')[0], '2026-11-08')

    def test_baseline_silent_and_meaningful_edits_coalesce_and_undo(self):
        self.changes_only()
        task = self.task(due_at=at(18))
        self.briefings.sync(at(9))
        self.assertEqual(self.briefings.view(at(9))['pending'], [])
        self.store.update(task['id'], dict(notes='备注', wake_at=at(19)))
        self.briefings.sync(at(9, 1))
        self.assertEqual(self.briefings.view(at(9, 1))['pending'], [])
        self.store.update(task['id'], dict(due_at=at(17)))
        self.briefings.sync(at(10))
        self.store.update(task['id'], dict(due_at=at(16)))
        self.briefings.sync(at(10, 1))
        self.assertIsNone(self.claim(at(10, 2)))
        pending = self.briefings.view(at(10, 2))['pending']
        self.assertEqual(len(pending), 1)
        self.assertIn('18:00', pending[0]['text'])
        self.assertIn('16:00', pending[0]['text'])
        self.assertNotIn('17:00', pending[0]['text'])
        self.store.update(task['id'], dict(due_at=at(18)))
        self.briefings.sync(at(10, 3))
        self.assertEqual(self.briefings.view(at(10, 3))['pending'], [])
        self.assertIsNone(self.claim(at(10, 6)))

    def test_cancel_delete_completion_and_move_out_of_horizon_are_reported(self):
        self.changes_only()
        cancelled = self.event(starts_at=at(16), ends_at=at(17))
        removed = self.event(starts_at=at(16), ends_at=at(17))
        moved = self.event(starts_at=at(16), ends_at=at(17))
        task = self.task()
        self.briefings.sync(at(9))
        self.store.update_event(cancelled['id'], dict(status='cancelled'))
        self.store.set_deleted('event', removed['id'], True)
        self.store.update_event(moved['id'], dict(starts_at=at(16, day=20), ends_at=at(17, day=20)))
        self.store.update(task['id'], dict(status='done'))
        self.briefings.sync(at(10))
        pending = self.briefings.view(at(10))['pending']
        self.assertEqual(len(pending), 4)
        self.assertIn('已取消', pending[0]['text'])
        self.assertIn('已删除', pending[1]['text'])
        self.assertIn('11-20', pending[2]['text'])
        self.assertIn('已完成', pending[3]['text'])

    def test_daily_once_per_local_day_and_old_unsent_briefing_expires(self):
        self.briefings.sync(at(9, day=6))
        self.assertIsNone(self.claim(at(9, day=6)))
        self.briefings.sync(at(9))
        history = self.briefings.view(at(9))['history']
        self.assertEqual(history[-1]['status'], 'expired')
        claimed = self.claim(at(10))
        self.briefings.result(claimed['id'], 'delivered', at(10))
        self.briefings.sync(at(11))
        self.assertIsNone(self.claim(at(11)))
        self.briefings.sync(at(10, day=8))
        self.assertEqual(self.claim(at(10, day=8))['kind'], 'daily')

    def test_delivery_respects_busy_buffers_quiet_hours_and_cooldown(self):
        self.event(starts_at=at(11), ends_at=at(12))
        self.briefings.sync(at(9))
        self.assertIsNone(self.claim(at(9)))
        self.assertIsNone(self.claim(at(10, 30)))
        self.assertIsNone(self.claim(at(12, 29)))
        daily = self.claim(at(12, 30))
        self.briefings.result(daily['id'], 'delivered', at(12, 30))
        self.task()
        self.briefings.sync(at(12, 31))
        self.assertIsNone(self.claim(at(12, 59)))
        changed = self.claim(at(13))
        self.assertEqual(changed['kind'], 'changes')
        self.briefings.result(changed['id'], 'delivered', at(13))
        self.task()
        self.briefings.sync(at(20, 59))
        self.assertIsNone(self.claim(at(21, 1)))

    def test_restart_does_not_resend_unknown_send(self):
        self.briefings.sync(at(10))
        item = self.claim(at(10))
        self.briefings = Briefings(TaskStore(self.store.path), wechat=self.wechat)
        self.briefings.sync(at(11))
        self.assertIsNone(self.claim(at(11)))
        self.assertEqual(self.row(item['id'])['status'], 'unknown')

    def test_retry_refreshes_changes_keeps_client_and_original_recipient(self):
        self.changes_only()
        task = self.task(due_at=at(18))
        self.briefings.sync(at(9))
        self.store.update(task['id'], dict(due_at=at(17)))
        self.briefings.sync(at(10))
        first = self.claim(at(10, 2))
        self.briefings.result(first['id'], 'retry', at(10, 2), 'known rejection')
        self.wechat.recipient_id = 'another-account'
        self.store.update(task['id'], dict(due_at=at(16)))
        self.briefings.sync(at(10, 3))
        self.assertIsNone(self.claim(at(10, 4)))
        retry = self.claim(at(10, 5))
        self.assertEqual(retry['client_id'], first['client_id'])
        self.assertEqual(retry['recipient_id'], 'self')
        self.assertIn('18:00', retry['body'])
        self.assertIn('16:00', retry['body'])
        self.assertNotIn('17:00', retry['body'])
        self.briefings.result(retry['id'], 'retry', at(10, 5))
        third = self.claim(at(10, 6))
        self.briefings.result(third['id'], 'retry', at(10, 6))
        self.assertEqual(self.row(third['id'])['status'], 'failed')
        self.assertIsNone(self.claim(at(11)))

    def test_retry_is_discarded_when_change_reverted(self):
        self.changes_only()
        task = self.task(due_at=at(18))
        self.briefings.sync(at(9))
        self.store.update(task['id'], dict(due_at=at(17)))
        self.briefings.sync(at(10))
        item = self.claim(at(10, 2))
        self.briefings.result(item['id'], 'retry', at(10, 2))
        self.store.update(task['id'], dict(due_at=at(18)))
        self.briefings.sync(at(10, 3))
        self.assertIsNone(self.claim(at(10, 5)))
        self.assertEqual(self.row(item['id'])['status'], 'superseded')
        self.assertEqual(self.briefings.view(at(10, 5))['pending'], [])
        self.assertIsNone(self.claim(at(11)))

    def test_pause_resume_builds_fresh_baseline_and_daily_can_resume(self):
        self.briefings.sync(at(9))
        task = self.task()
        self.briefings.sync(at(9, 1))
        self.assertTrue(self.briefings.view(at(9, 1))['pending'])
        self.briefings.configure(dict(daily_enabled=False, changes_enabled=False))
        self.store.update(task['id'], dict(due_at=at(17)))
        self.briefings.configure(dict(changes_enabled=True))
        self.briefings.sync(at(10))
        self.assertEqual(self.briefings.view(at(10))['pending'], [])
        self.assertIsNone(self.claim(at(10)))
        self.briefings.configure(dict(daily_enabled=True))
        self.briefings.sync(at(10, 1))
        self.assertEqual(self.claim(at(10, 1))['kind'], 'daily')

    def test_followup_sends_concise_summary_and_private_link_with_no_duplicate(self):
        followup = FollowUp(self.store, wechat=self.wechat, public_url='https://assistant.example')
        for i in range(5):
            self.task()
        followup.briefings.sync(at(10))
        with self.store.worker_lock():
            followup.send_briefing(at(10))
            followup.send_briefing(at(11))
        self.wechat.send.assert_called_once()
        text = self.wechat.send.call_args.args[0]
        self.assertIn('（5 条）', text)
        self.assertIn('其余安排请查看完整简报', text)
        self.assertIn('/calendar/' + self.store.calendar_token(), text)
        self.assertEqual(self.wechat.send.call_args.kwargs['recipient_id'], 'self')
        self.assertEqual(self.briefings.view(at(11))['history'][0]['status'], 'delivered')
        view = calendar_view(SimpleNamespace(store=self.store, followup=followup))['briefings']
        safe = json.dumps(view)
        for private in ('client_id', 'recipient_id', 'bot_id', 'snapshot'):
            self.assertNotIn(private, safe)

    def test_followup_known_retry_and_unknown_exception_have_distinct_outcomes(self):
        followup = FollowUp(self.store, wechat=self.wechat)
        followup.briefings.sync(at(10))
        self.wechat.send.side_effect = PushError('rejected', status='retry')
        with self.store.worker_lock():
            followup.send_briefing(at(10))
        self.assertEqual(self.briefings.view(at(10))['history'][0]['status'], 'retry')
        self.wechat.send.side_effect = TimeoutError('transport interrupted')
        with self.store.worker_lock():
            followup.send_briefing(at(10, 1))
        self.assertEqual(self.briefings.view(at(10, 1))['history'][0]['status'], 'unknown')

    def test_normal_tick_prepares_briefing_and_does_not_send_at_night(self):
        followup = FollowUp(self.store, wechat=self.wechat)
        followup.receive_wechat = Mock()
        followup.tick(now=at(1))
        self.assertEqual(followup.last_tick, at(1))
        self.assertEqual(self.briefings.view(at(1))['history'][0]['status'], 'pending')
        self.wechat.send.assert_not_called()
        followup.tick(now=at(10))
        self.wechat.send.assert_called_once()
        self.assertEqual(self.briefings.view(at(10))['history'][0]['status'], 'delivered')

    def test_private_mobile_api_requires_token_and_same_origin_for_settings(self):
        followup = FollowUp(self.store)
        server = task_server(SimpleNamespace(store=self.store, followup=followup), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        token = self.store.calendar_token()
        path = '/calendar/' + token

        def request(method, suffix, body=None, origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            connection.request(method, suffix, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, response.read().decode()

        self.assertEqual(request('GET', '/calendar/' + 'x' * 43 + '/data')[0], 404)
        self.assertEqual(request('POST', path + '/briefings', {'daily_enabled': False},
                                 'https://another.example')[0], 403)
        code, body = request('POST', path + '/briefings', {'daily_enabled': False})
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)['daily_enabled'])
        code, body = request('GET', path + '/data')
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)['briefings']['config']['daily_enabled'])
        self.assertIn('今日简报', request('GET', path)[1])


if __name__ == '__main__':
    unittest.main()
