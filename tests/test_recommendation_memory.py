"""Memory selection and durable recommendation delivery, without live messages."""

from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from personal_assistant.personalization import next_recommendation_time
from personal_assistant.proactive import ProactiveWorker
from personal_assistant.store import TaskStore
from personal_context.recall import MEMORY_FIELDS, recall


def at(hour, minute=0, day=6):
    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class RecommendationMemoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'assistant.db'
        self.store = TaskStore(self.path)
        self.store.set_settings(dict(self.store.settings(), dh_knowledge=[]))
        self.store.set_personalization(dict(enabled=True))
        self.context = dict(chat_profiles=[], dh_facts=[], memories=[])
        self.wechat = SimpleNamespace(recipient_id='self', bot_id='test')

    def save(self, key, *, now=None, title='候选餐厅', address='一号路1号',
             url='https://places.example/1', context=None, items=None, before_finish=None):
        now = at(12) if now is None else now
        trigger = dict(kind='travel_prepare', expires_at=at(20), target_date='2026-11-06')
        versions = self.store.recommendation_context(now)
        self.assertTrue(self.store.start_proactive(key, trigger, versions, day='2026-11-06', now=now))
        if before_finish:
            before_finish()
        output = dict(summary='找到适合的去处', items=items if items is not None else [
            dict(title=title, body='行程附近可选', reason='特色菜值得顺路尝试', url=url,
                 profile_ids=[], dh_fact_ids=[], discovery=dict(category='restaurant', address=address))])
        return self.store.finish_proactive(key, trigger, versions, context or self.context,
                                          output, wechat=self.wechat, now=now)

    def row(self, insight_id):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM insights WHERE id=?', (insight_id,)).fetchone())

    def test_recall_keeps_relevant_interests_and_whole_dietary_constraints_within_budget(self):
        memories = [dict(memory_id=f'other:{i}', field='preference', value='喜欢跑步健身', observed_at=100+i)
                    for i in range(80)]
        restriction = dict(memory_id='diet', field='dietary_preference', value='花生过敏，含花生和花生油的菜都不能吃',
                           observed_at=1)
        interest = dict(memory_id='art', field='interest', value='喜欢摄影展和纪实摄影', observed_at=2)
        memories.extend([restriction, interest])
        selected = recall(memories, '摄影展', categories=['restaurant', 'exhibition'], limit=4, max_chars=700)
        self.assertEqual(selected[0], restriction)
        self.assertIn(interest, selected)
        self.assertLessEqual(len(selected), 4)
        self.assertLessEqual(sum(len(json.dumps({k: m[k] for k in MEMORY_FIELDS if k in m},
                                                ensure_ascii=False)) for m in selected), 700)
        self.assertEqual(len(memories), 82)

    def test_requested_research_persists_after_automatic_budget_is_used(self):
        now = at(12)
        trigger = dict(kind='travel_prepare', expires_at=at(20), target_date='2026-11-06', anchor='event')
        versions = self.store.recommendation_context(now)
        for key in ('automatic-one', 'automatic-two'):
            self.assertTrue(self.store.start_proactive(key, trigger, versions, day='2026-11-06', now=now))
        self.assertFalse(self.store.start_proactive('automatic-three', trigger, versions,
                                                   day='2026-11-06', now=now))
        worker = ProactiveWorker(self.store, wechat=self.wechat, maps=Mock())
        worker.prepare = Mock(return_value=(self.context, versions,
                              dict(timezone='Asia/Shanghai'), [trigger]))
        worker.research = AsyncMock(return_value=dict(summary='本次请求结果', items=[dict(
            title='展览', body='有效展期内的展览', reason='有具体特色且适合本次行程',
            url='https://exhibition.example/1', profile_ids=[], dh_fact_ids=[])]))
        with patch('personal_assistant.proactive.time.time', return_value=now):
            result = worker.tick(Event(), now=now, requested=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(result['insight_ids']), 1)
        self.assertEqual(self.row(result['insight_ids'][0])['title'], '展览')
        self.assertFalse(self.store.start_proactive('automatic-four', trigger, versions,
                                                   day='2026-11-06', now=now))

    def test_requested_run_does_not_consume_automatic_budget(self):
        now = at(12)
        trigger = dict(kind='travel_prepare')
        versions = self.store.recommendation_context(now)
        for key, requested in [('requested-one', True), ('automatic-one', False),
                               ('requested-two', True), ('automatic-two', False)]:
            self.assertTrue(self.store.start_proactive(key, trigger, versions,
                day='2026-11-06', now=now, requested=requested))
        self.assertFalse(self.store.start_proactive('automatic-three', trigger, versions,
                                                   day='2026-11-06', now=now))

    def test_delivery_window_uses_ordinary_recurring_events_and_deletion(self):
        event = self.store.create_event(title='固定活动', starts_at=at(11), ends_at=at(14),
            recurrence=dict(frequency='weekly', weekdays=[4]), timezone='Asia/Shanghai')
        self.assertEqual(next_recommendation_time(self.store.events(), at(12), at(20), 'Asia/Shanghai'), at(14, 30))
        self.assertIsNone(next_recommendation_time(self.store.events(), at(12), at(14), 'Asia/Shanghai'))
        self.assertEqual(next_recommendation_time(self.store.events(), at(9, day=7), at(20, day=7),
                                                  'Asia/Shanghai'), at(10, day=7))
        self.store.set_deleted('event', event['id'], True)
        self.assertEqual(next_recommendation_time(self.store.events(), at(12), at(20), 'Asia/Shanghai'), at(12))

    def test_waiting_recommendation_survives_restart_without_a_send_attempt(self):
        self.store.create_event(title='活动', starts_at=at(11), ends_at=at(14), timezone='Asia/Shanghai')
        insight_id = self.save('waiting')[0]
        self.assertEqual(self.row(insight_id)['not_before'], at(14, 30))
        self.store = TaskStore(self.path)
        self.assertIsNone(self.store.claim_insight(at(12, 1)))
        self.assertEqual(self.row(insight_id)['notification_attempts'], 0)
        self.assertEqual(self.store.claim_insight(at(14, 30))['id'], insight_id)
        self.assertEqual(self.row(insight_id)['notification_attempts'], 1)

    def test_rejection_survives_restart_and_blocks_changed_links_during_research(self):
        first = self.save('first')[0]
        self.store.read_insight(first)
        self.store = TaskStore(self.path)
        # Feedback arriving while research is running must still affect its final save.
        ids = self.save('second', now=at(12, 1), before_finish=lambda: self.store.dismiss_insight(first), items=[
            dict(title='候选餐厅', body='', reason='', url='https://another-platform.example/changed',
                 profile_ids=[], dh_fact_ids=[], discovery=dict(category='restaurant', address='一号路1号')),
            dict(title='另一家餐厅', body='', reason='另一处合适的选择', url='https://places.example/2',
                 profile_ids=[], dh_fact_ids=[], discovery=dict(category='restaurant', address='二号路2号'))])
        self.assertEqual([self.row(i)['title'] for i in ids], ['另一家餐厅'])
        self.assertEqual(self.store.rejected_recommendations()[0]['title'], '候选餐厅')
        self.assertEqual(self.row(first)['feedback'], 'not_interested')
        self.assertEqual(self.store.profiles(), [])  # No inferred dislike of an entire cuisine.

    def test_external_knowledge_changes_invalidate_queued_recommendations(self):
        knowledge = Path(self.directory.name) / 'knowledge.json'
        knowledge.write_text('{}')
        self.store.set_settings(dict(self.store.settings(), dh_knowledge=[str(knowledge)]))
        insight_id = self.save('before-change')[0]
        knowledge.write_text('{"changed": true}')
        self.assertIsNone(self.store.claim_insight(at(12, 1)))
        self.assertEqual(self.row(insight_id)['notification_status'], 'superseded')

    def test_recommendation_policy_changes_invalidate_queue_without_teaching_a_dislike(self):
        insight_id = self.save('older-policy')[0]
        with self.store.connect() as db:
            version = json.loads(db.execute('SELECT context_json FROM insights WHERE id=?',
                                            (insight_id,)).fetchone()[0])
            version.pop('recommendation_policy')
            db.execute('UPDATE insights SET context_json=? WHERE id=?', (json.dumps(version), insight_id))
        self.assertIsNone(self.store.claim_insight(at(12, 1)))
        self.assertEqual(self.row(insight_id)['notification_status'], 'superseded')
        self.assertNotEqual(self.row(insight_id)['feedback'], 'not_interested')
        self.assertEqual(self.store.rejected_recommendations(), [])

    def test_referenced_memory_expiry_stops_delivery(self):
        context = dict(self.context, dh_facts=[dict(id='temporary-interest')], memories=[
            dict(dh_fact_id='temporary-interest', valid_until=at(13))])
        items = [dict(title='展览', body='', reason='适合近期兴趣', url='https://exhibition.example/1',
                      profile_ids=[], dh_fact_ids=['temporary-interest'], discovery={})]
        insight_id = self.save('temporary', context=context, items=items)[0]
        self.assertEqual(self.row(insight_id)['expires_at'], at(13))
        self.assertIsNone(self.store.claim_insight(at(13)))
        self.assertEqual(self.row(insight_id)['notification_status'], 'expired')


if __name__ == '__main__':
    unittest.main()
