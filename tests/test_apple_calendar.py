"""Calendar reconciliation uses structured snapshots, without live account access."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from app import Assistant
from briefing import Briefings
from followup import FollowUp
from inputs.apple_calendar import AppleCalendar, CalendarReadError
from memory.calendar import CalendarMemory
from memory.store import TaskStore
from presentation.task_web import calendar_view, event_view


def at(day, hour=0, timezone='Asia/Shanghai'):
    return datetime(2026, 11, day, hour, tzinfo=ZoneInfo(timezone)).timestamp()


START, END, NOW = at(1), at(30), at(8, 8)


def event(**changes):
    return dict(id='external', calendar_id='calendar-one', occurrence=None, title='会面',
        notes='', location='会议室', starts_at=at(8, 14), ends_at=at(8, 15),
        all_day=False, timezone='Asia/Shanghai', status='confirmed', remind_before_minutes=30) | changes


def snapshot(*events):
    return dict(complete=True, start=START, end=END,
        calendars=[dict(id='calendar-one', name='日程', account='私人账户'),
                   dict(id='calendar-two', name='工作', account='其他账户')], events=list(events))


class AppleCalendarTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / 'assistant.db')
        self.memory = CalendarMemory(self.store)

    def apply(self, value, **kwargs):
        return self.memory.apply(value, start=START, end=END, now=NOW, **kwargs)

    def test_replay_move_cancel_and_restore_preserve_identity(self):
        value = snapshot(event())
        self.assertEqual(self.apply(value), dict(created=1, updated=0, cancelled=0))
        original = self.store.events()[0]
        self.assertEqual(self.apply(value), dict(created=0, updated=0, cancelled=0))
        self.assertEqual(self.store.event(original['id'])['revision'], original['revision'])
        value['events'][0].update(starts_at=at(9, 14), ends_at=at(9, 15), timezone='UTC')
        self.assertEqual(self.apply(value)['updated'], 1)
        moved = self.store.events()[0]
        self.assertEqual(moved['id'], original['id'])
        self.assertEqual(moved['timezone'], 'UTC')
        self.assertEqual(self.apply(snapshot())['cancelled'], 1)
        self.assertEqual(self.store.events()[0]['status'], 'cancelled')
        self.apply(value)
        self.assertEqual(self.store.events()[0]['status'], 'confirmed')
        value['events'][0]['status'] = 'cancelled'
        self.apply(value)
        self.assertEqual(self.store.events()[0]['status'], 'cancelled')

    def test_recurrence_occurrence_and_calendar_ids_separate_shared_external_id(self):
        value = snapshot(event(occurrence=at(8, 14)),
            event(occurrence=at(9, 14), starts_at=at(9, 14), ends_at=at(9, 15)),
            event(calendar_id='calendar-two'))
        self.apply(value)
        ids = [e['id'] for e in self.store.events()]
        value['events'][0].update(starts_at=at(8, 16), ends_at=at(8, 17))
        self.apply(value)
        self.assertEqual({e['id'] for e in self.store.events()}, set(ids))
        self.assertEqual(len(self.store.event_occurrences(at(8), at(10))), 3)
        value['events'].pop(1)
        self.assertEqual(self.apply(value)['cancelled'], 1)

    def test_all_day_exclusive_end_and_point_event_are_preserved(self):
        self.apply(snapshot(event(starts_at=at(8), ends_at=at(10), all_day=True),
                            event(id='point', starts_at=at(8, 12), ends_at=at(8, 12))))
        all_day = next(e for e in self.store.events() if e['all_day'])
        self.assertEqual(all_day['ends_at'], at(10))
        point = next(e for e in self.store.events() if not e['all_day'])
        self.assertEqual(point['ends_at'], point['starts_at'])
        self.assertEqual(len(self.store.event_occurrences(at(10), at(11))), 0)
        self.assertEqual(len(self.store.event_occurrences(at(8, 13), at(8, 14))), 1)

    def test_bad_snapshot_rolls_back_and_missing_account_never_mass_cancels(self):
        self.apply(snapshot(event()))
        for value in (dict(snapshot(), complete=False),
                      dict(snapshot(), calendars=[]),
                      snapshot(event(title='Changed'), event(id='bad', ends_at=at(7)))):
            with self.assertRaises(ValueError):
                self.apply(value)
            self.assertEqual(self.store.events()[0]['title'], '会面')
            self.assertEqual(self.store.events()[0]['status'], 'confirmed')

    def test_manual_and_outside_window_events_are_untouched(self):
        manual = self.store.create_event(title='手动日程', starts_at=at(8), ends_at=at(9))
        self.apply(snapshot(event()))
        narrower = snapshot()
        narrower.update(start=at(10))
        self.memory.apply(narrower, start=at(10), end=END, now=NOW)
        self.assertTrue(all(e['status'] == 'confirmed' for e in self.store.events()))
        self.assertEqual(self.store.event(manual['id'])['scope'], {})

    def test_calendar_selection_deactivates_deselected_and_restores_on_return(self):
        value = snapshot(event(), event(calendar_id='calendar-two'))
        self.apply(value)
        self.apply(value, calendar_ids=['calendar-two'])
        self.assertEqual([e['scope']['calendar_id'] for e in self.store.events() if e['status'] == 'confirmed'], ['calendar-two'])
        self.apply(value)
        self.assertEqual(len([e for e in self.store.events() if e['status'] == 'confirmed']), 2)

    def test_imported_events_refuse_all_ui_mutations_and_hide_private_source_ids(self):
        self.apply(snapshot(event()))
        imported = self.store.events()[0]
        app = Assistant(self.store.path)
        for change in (dict(title='改名'), dict(status='cancelled'), dict(deleted=True), dict(deleted=False)):
            with self.assertRaisesRegex(ValueError, 'Apple 日历'):
                app.update_event(imported['id'], change)
        manual = app.create_event(dict(title='自己填写'))
        self.assertEqual(app.update_event(manual['id'], dict(title='可编辑'))['title'], '可编辑')
        view = event_view(imported)
        self.assertTrue(view['read_only'])
        self.assertEqual(view['source']['name'], '日程')
        for private in ('calendar-one', 'external', 'calendar_key', '私人账户'):
            self.assertNotIn(private, json.dumps(view))
        self.assertEqual(calendar_view(app)['events'][0]['read_only'], True)

    def worker(self, value):
        reader = Mock()
        def read(start, end):
            return dict(deepcopy(value), start=start, end=end)
        reader.read.side_effect = read
        worker = AppleCalendar(self.store, reader=reader)
        worker.configure(enabled=True)
        return worker

    def test_poll_throttle_persists_and_read_failures_preserve_events_and_reminders(self):
        worker = self.worker(snapshot(event()))
        worker.sync(now=NOW)
        AppleCalendar(self.store, reader=worker.reader).sync(now=NOW + 60)
        worker.reader.read.assert_called_once()
        worker.reader.read.side_effect = CalendarReadError('需要日历权限')
        followup = FollowUp(self.store)
        followup.calendar = worker
        task = self.store.create(description='仍需跟进', wake_at=NOW)
        followup.tick(now=NOW + 301)
        self.assertEqual(worker.memory.status()['error'], '需要日历权限')
        self.assertEqual(self.store.events()[0]['status'], 'confirmed')
        self.assertEqual(len(self.store.inbox(task_id=task['id'])), 1)
        self.assertEqual(followup.last_tick, NOW + 301)

    def test_first_import_baselines_only_its_events_then_source_edits_trigger_changes(self):
        briefing = Briefings(self.store)
        briefing.configure(dict(daily_enabled=False))
        task = self.store.create(description='尚未完成', due_at=at(8, 20))
        briefing.sync(NOW)
        self.store.update(task['id'], dict(due_at=at(8, 19)))
        worker = self.worker(snapshot(event()))
        worker.sync(now=NOW)
        briefing.sync(NOW + 1)
        pending = briefing.view(NOW + 1)['pending']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['kind'], 'task')
        worker.reader.read.side_effect = lambda start, end: dict(snapshot(event(location='新地点')), start=start, end=end)
        worker.sync(now=NOW + 301)
        briefing.sync(NOW + 302)
        self.assertEqual({c['kind'] for c in briefing.view(NOW + 302)['pending']}, {'task', 'event'})
        self.assertIn('会面', briefing.view(NOW)['today']['body'])

    def test_disabled_tick_never_calls_native_reader(self):
        worker = AppleCalendar(self.store, reader=Mock())
        worker.sync(now=NOW)
        worker.reader.read.assert_not_called()
