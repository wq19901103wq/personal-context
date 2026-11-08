"""Business regressions for intent, saved context and withdrawing false extractions."""

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
    def test_shared_originals_are_loaded_once_and_keep_task_event_updates(self):
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory)/'assistant.db')
            shared, detail, fresh = (record(1, '请发送付款凭证'), record(2, '周五一起核对'),
                                     record(3, '好，我补充发送，再一起核对'))
            first = store.create(description='发送凭证', scope=SCOPE)
            second = store.create(description='补充附件', scope=SCOPE)
            event = store.create_event(title='核对付款', scope=SCOPE)
            with store.connect() as db:
                for kind, item, records in [('task', first, [shared]), ('task', second, [shared]),
                                            ('event', event, [shared, detail])]:
                    for r in records:
                        db.execute('INSERT INTO item_evidence VALUES (?,?,?,?,?,?,?)',
                            (kind, item['id'], source_key(SCOPE), r.id, r.content_hash,
                             json.dumps(asdict(r)), r.time.recorded_at))
            prompts = []
            changed = task(3, task_id=first['id'])
            changed['evidence_record_ids'] = ['1', '3']
            changed_event = dict(event_id=event['id'], title='共同核对付款', notes='', reason='',
                intent='action', anchor_record_id='3', evidence_record_ids=['2', '3'],
                involvement=dict(state='confirmed', self_role='核对付款', evidence_record_ids=['3']),
                time_action='keep', starts_at=None, ends_at=None, scheduled_date=None, time_period='',
                all_day=False, location=None, time_text='', status=None)

            class Runtime:
                async def create_thread(self):
                    pass

                async def run_turn(self, prompt, **kwargs):
                    prompts.append(json.loads(prompt))
                    return SimpleNamespace(status='completed', final_message=json.dumps(
                        output(changed, events=[changed_event])))

                async def close(self):
                    pass

            worker = ExtractionWorker(store, source_loader=lambda *_: RecordBatch((fresh,)),
                                      runtime_factory=Runtime)
            store.queue_scan(source_key(SCOPE), SCOPE, 24)
            queries, connect = [], store.connect

            @contextmanager
            def traced_connect():
                with connect() as db:
                    db.set_trace_callback(queries.append)
                    yield db

            with patch.object(store, 'connect', traced_connect):
                result = asyncio.run(worker.extract(store.claim_scan(), Event()))
            reads = [q for q in queries if q.startswith('SELECT ') and 'FROM item_evidence' in q]
            self.assertEqual(len(reads), 2)  # One task batch and the already-loaded event batch.
            self.assertEqual(result['updated_task_ids'], [first['id']])
            self.assertEqual(result['updated_event_ids'], [event['id']])
            prompt = prompts[0]
            self.assertEqual(prompt['records'], [asdict(shared), asdict(detail), asdict(fresh)])
            self.assertEqual(prompt['related_records'], [])
            self.assertEqual(prompt['new_record_ids'], ['3'])
            self.assertTrue(all(t['original_record_refs'] == ['records[0]']
                                for t in prompt['existing_tasks']))
            self.assertEqual(prompt['existing_events'][0]['original_record_refs'], ['records[0]', 'records[1]'])
            self.assertTrue(all('original_records' not in item
                                for item in prompt['existing_tasks'] + prompt['existing_events']))
            self.assertEqual(store.event(event['id'])['title'], changed_event['title'])
            self.assertEqual({r['id'] for r in store.evidence('task', first['id'])}, {'1', '3'})

    def test_context_refs_separate_chats_and_versions_with_the_same_record_id(self):
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory)/'assistant.db')
            saved = record(1, '旧的付款说明')
            fresh = replace(saved, text='更新后的付款说明', content_hash='edited-version')
            foreign = replace(saved, stream_id='other-chat')
            current = store.create(description='本会话事项', scope=dict(SCOPE,
                origin=dict(records=[asdict(saved)])))
            for title in ('另一个会话事项一', '另一个会话事项二'):
                store.create(description=title, scope=dict(SCOPE, chat_id='other-chat',
                    origin=dict(records=[asdict(foreign)])))
            excluded = store.create(description='另一账号事项', scope=dict(SCOPE,
                history_account='other-account', origin=dict(records=[asdict(saved)])))
            prompts = []

            class Runtime:
                async def create_thread(self):
                    pass

                async def run_turn(self, prompt, **kwargs):
                    prompts.append(json.loads(prompt))
                    return SimpleNamespace(status='completed', final_message=json.dumps(
                        output(task(1, task_id=current['id']))))

                async def close(self):
                    pass

            worker = ExtractionWorker(store, source_loader=lambda *_: RecordBatch((fresh,)),
                                      runtime_factory=Runtime)
            store.queue_scan(source_key(SCOPE), SCOPE, 24)
            result = asyncio.run(worker.extract(store.claim_scan(), Event()))
            self.assertEqual(result['updated_task_ids'], [current['id']])
            prompt = prompts[0]
            self.assertEqual(prompt['records'], [asdict(fresh)])
            self.assertEqual(prompt['related_records'], [asdict(saved), asdict(foreign)])
            snapshots = {t['id']: t for t in prompt['existing_tasks']}
            self.assertNotIn(excluded['id'], snapshots)
            self.assertEqual(snapshots[current['id']]['original_record_refs'], ['related_records[0]'])
            self.assertTrue(all(t['original_record_refs'] == ['related_records[1]']
                                for t in snapshots.values() if t['chat_id'] == 'other-chat'))
            self.assertEqual(store.evidence('task', current['id']), [asdict(fresh)])

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
            self.assertEqual(prompts[0]['existing_tasks'][0]['original_record_refs'], ['records[0]'])
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
