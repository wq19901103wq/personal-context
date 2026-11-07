"""Business regressions for intent, saved context and withdrawing false extractions."""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest

from personal_assistant.extraction import ExtractionWorker, INSTRUCTIONS, source_key
from personal_assistant.execution import build_runtime
from personal_assistant.store import TaskStore
from personal_context.history import MessageTime, SenderEvidence, SourceBinding
from personal_context.records import RawRecord, RecordBatch


SCOPE = dict(history_source='sample', history_account='self', chat_id='group',
             chat_name='讨论群', chat_type='group', timezone='Asia/Shanghai')


def record(rid, text, is_self=True):
    return RawRecord(str(rid), SourceBinding('sample', 'self'), 'group', 'wechat_message',
        SenderEvidence('self' if is_self else 'peer', '我' if is_self else '同事', 'source_id', is_self),
        MessageTime(1800000000 + rid, None, 1800000000 + rid), text, f'hash-{rid}')


def task(rid, *, intent='action', task_id=None):
    return dict(task_id=task_id, description='将付款凭证发给财务', notes='', reason='本人接受的实际请求',
        intent=intent, anchor_record_id=str(rid), evidence_record_ids=[str(rid)],
        involvement=dict(state='confirmed', self_role='发送凭证', evidence_record_ids=[str(rid)]),
        status=None, due_action='keep', due_at=None, due_text='')


def output(*tasks, events=()):
    return dict(summary='', tasks=list(tasks), events=list(events), profiles=[], insights=[])


class ExtractionIntentTest(unittest.TestCase):
    def test_intent_is_independent_of_participation(self):
        records = [record(1, '先记下来，到时看看谁打脸'), record(2, '好，付款凭证我发给财务')]
        for intent in ('banter', 'hypothetical', 'uncertain'):
            with self.subTest(intent=intent):
                tasks, *_ = ExtractionWorker.candidates(SCOPE, records,
                    output(task(1, intent=intent), task(2)), [], ['1', '2'])
                self.assertEqual([t['anchor_message_id'] for t in tasks], ['2'])

    def test_review_uses_saved_originals_and_stops_false_reminders(self):
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory)/'assistant.db')
            original = record(1, '先记下来，到时看看谁打脸')
            old = store.create(description='截图留存以便日后核算', notes='原先的错误内容',
                scope=dict(SCOPE, origin=dict(records=[asdict(original)])))
            foreign = store.create(description='其他会话事项', scope=dict(SCOPE,
                chat_id='other-chat', origin=dict(records=[asdict(record(9, '别的会话内容'))])))
            good = store.create_event(title='出行住宿', scope=dict(SCOPE,
                involvement=dict(state='confirmed')))
            manual = store.create_event(title='固定行程', recurrence=dict(frequency='weekly'))
            store.enqueue_execution(old['id'], '继续')
            store.commit_check(store.get(old['id']), observed=[], now=1800000000,
                reminder=dict(title='旧提醒', body='旧内容'), notify=True,
                wechat=SimpleNamespace(recipient_id='self', bot_id='bot'))
            with store.connect() as db:
                db.execute('INSERT INTO history_scan_state VALUES (?,?)',
                           (source_key(SCOPE), '{"preserved":"checkpoint"}'))
            prompts = []

            class Runtime:
                async def create_thread(self):
                    pass

                async def run_turn(self, prompt, **kwargs):
                    prompts.append(json.loads(prompt))
                    return SimpleNamespace(status='completed', final_message=json.dumps(
                        output(task(1, task_id=old['id'], intent='banter'), task(1))))

                async def close(self):
                    pass

            worker = ExtractionWorker(store, source_loader=lambda *_: RecordBatch((record(8, '无关的新聊天'),)),
                                      runtime_factory=Runtime)
            worker.queue_review(['group'])
            result = asyncio.run(worker.extract(store.claim_scan(), Event()))
            self.assertEqual(result['discarded_task_ids'], [old['id']])
            self.assertEqual(result['created_task_ids'], [])
            self.assertEqual(prompts[0]['records'][0]['text'], original.text)
            self.assertEqual(len(prompts[0]['records']), 1)
            self.assertEqual([t['id'] for t in prompts[0]['existing_tasks']], [old['id']])
            self.assertEqual([t['id'] for t in store.tasks()], [foreign['id']])
            self.assertEqual(store.get(old['id'])['notes'], '')
            self.assertEqual(store.scan_signature(source_key(SCOPE)), '{"preserved":"checkpoint"}')
            self.assertEqual({e['id'] for e in store.events()}, {good['id'], manual['id']})
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT notification_status FROM reminders').fetchone()[0], 'superseded')
                self.assertEqual(db.execute('SELECT status FROM wechat_deliveries').fetchone()[0], 'superseded')
                self.assertEqual(db.execute('SELECT stop_requested FROM executions').fetchone()[0], 1)

    def test_review_preserves_deleted_uncertain_manual_and_concurrently_edited_items(self):
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory)/'assistant.db')
            records = [record(1, '顺着说的玩笑')]
            old = store.create(description='旧提取', scope=dict(SCOPE, origin=dict(records=[])))
            wrong_actor = task(1, task_id=old['id'], intent='uncertain')
            wrong_actor['involvement']['state'] = 'other'
            tasks, *_ = ExtractionWorker.candidates(SCOPE, records,
                output(wrong_actor), [], [], [old])
            self.assertTrue(tasks[0]['discard'])
            tasks, *_ = ExtractionWorker.candidates(SCOPE, records,
                output(task(1, task_id=old['id'], intent='banter')), [], [], [old])
            store.update(old['id'], dict(notes='用户编辑'))
            with store.connect() as db:
                self.assertEqual(store.apply_extracted_in(db, dict(source_key=source_key(SCOPE)),
                    'task', tasks[0], now=1800000010)[1], 'skipped')
            self.assertIsNone(store.get(old['id'])['deleted_at'])
            for intent, row in [('uncertain', store.get(old['id'])),
                                ('banter', store.create(description='手工事项', scope=SCOPE))]:
                tasks, *_ = ExtractionWorker.candidates(SCOPE, records,
                    output(task(1, task_id=row['id'], intent=intent)), [], [], [row])
                self.assertEqual(tasks, [])
            store.set_deleted('task', old['id'], True)
            tasks, *_ = ExtractionWorker.candidates(SCOPE, records,
                output(task(1, task_id=old['id'])), [], ['1'], [store.get(old['id'])])
            self.assertEqual(tasks, [])

    def test_legacy_correction_text_is_removed_without_deleting_manual_schedule(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'assistant.db'
            store = TaskStore(path)
            old = store.create(description='误项', notes='旧纠错说明',
                               scope=dict(SCOPE, user_correction='旧纠错说明'))
            store.set_deleted('task', old['id'], True)
            manual = store.create_event(title='固定行程', scope=dict(user_correction=True, source='user'))
            store = TaskStore(path)
            self.assertEqual(store.get(old['id'])['notes'], '')
            self.assertNotIn('user_correction', store.get(old['id'])['scope'])
            self.assertIsNotNone(store.get(old['id'])['deleted_at'])
            self.assertIsNone(store.event(manual['id'])['deleted_at'])
            self.assertEqual(len(list((path.parent/'backups').glob('*.db'))), 1)


@unittest.skipUnless(os.environ.get('PERSONAL_ASSISTANT_LIVE_TEST'), 'opt-in model regression')
class LiveIntentTest(unittest.TestCase):
    def test_banter_and_actual_requests_with_similar_action_words(self):
        from personal_assistant.cli import DEFAULT_DB
        worker = ExtractionWorker(SimpleNamespace(path=DEFAULT_DB), runtime_factory=lambda: build_runtime(
            SimpleNamespace(path=DEFAULT_DB), INSTRUCTIONS, model='gpt-6-luna'))
        records = [record(1, '我这次一定一把翻身', False),
                   record(2, '先截图留着，下周看看是谁打脸'),
                   record(3, '别到时候只有教训'),
                   record(4, '财务今天要结账，请你把项目付款截图发给她核销', False),
                   record(5, '好的，付款截图我来发，还没发')]
        prompt = dict(stream=dict(name='讨论群', type='group'), timezone=SCOPE['timezone'],
            current_time='2027-01-15T08:00:00+08:00', incremental_only=False, review_existing=False,
            existing_tasks=[], existing_events=[], existing_profiles=[], new_record_ids=[r.id for r in records],
            records=[asdict(r) for r in records])
        result = asyncio.run(worker.infer(prompt, Event()))
        self.assertEqual(len(result['tasks']), 1)
        item = result['tasks'][0]
        self.assertEqual(item['intent'], 'action')
        self.assertTrue({'4', '5'}.intersection(item['evidence_record_ids']))
        self.assertFalse({'1', '2', '3'}.intersection(item['evidence_record_ids']))
        self.assertEqual(result['events'], [])


if __name__ == '__main__':
    unittest.main()
