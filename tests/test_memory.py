"""Persistent input progress and the current personal-memory view."""

import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest

from personal_assistant.extraction import ExtractionWorker, source_key
from personal_assistant.execution import build_runtime
from personal_assistant.personalization import PersonalContext
from personal_assistant.proactive import ProactiveWorker
from personal_assistant.store import TaskStore
from personal_context.assistant_knowledge import open_assistant_knowledge
from personal_context.history import MessageTime, SenderEvidence, SourceBinding
from personal_context.records import RawRecord, RecordBatch


SCOPE = dict(history_source='sample', history_account='self', chat_id='group',
             chat_name='聊天', chat_type='group', timezone='Asia/Shanghai')
NOW = 1800000000


def record(rid, text='喜欢看展', *, at=NOW, is_self=True):
    return RawRecord(str(rid), SourceBinding('sample', 'self'), 'group', 'wechat_message',
        SenderEvidence('self' if is_self else 'peer', '我' if is_self else '朋友', 'source_id', is_self),
        MessageTime(at, None, at), text, f'{rid}:{text}')


def profile(rid, value='喜欢看展', **changes):
    return dict(dict(subject_record_id=str(rid), category='interest', field='interest', value=value,
        confidence='explicit', evidence_record_ids=[str(rid)], profile_id=None, operation='remember',
        temporal_kind='durable', valid_from=None, valid_until=None, time_text=''), **changes)


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'assistant.db'
        self.store = TaskStore(self.path)
        self.store.set_settings(dict(self.store.settings(), dh_knowledge=[]))

    def scan(self, scope=SCOPE):
        self.store.queue_scan(source_key(scope), scope, 24)
        return self.store.claim_scan()

    def save(self, *profiles, records=None, scope=SCOPE):
        records = records or [record(1)]
        output = dict(summary='', tasks=[], events=[], profiles=list(profiles), insights=[])
        _, _, facts, _ = ExtractionWorker.candidates(scope, records, output,
            self.store.profiles(include_dismissed=True), [r.id for r in records])
        return self.store.finish_scan(self.scan(scope), profiles=facts)['profile_ids']

    def test_processed_messages_survive_window_restart_and_content_edits(self):
        first, second = record(1), record(2)
        key = source_key(SCOPE)
        self.store.finish_scan(self.scan(), processed_records=[first], signature=json.dumps({'1': first.content_hash}))
        self.store.finish_scan(self.scan(), processed_records=[second], signature=json.dumps({'2': second.content_hash}))
        store = TaskStore(self.path)
        self.assertEqual(store.unprocessed_records(key, [first, second]), [])
        changed = replace(first, text='现在喜欢摄影展', content_hash='new-version')
        self.assertEqual(store.unprocessed_records(key, [changed, second]), ['1'])
        other = source_key(dict(SCOPE, chat_id='other'))
        self.assertEqual(store.unprocessed_records(other, [first]), ['1'])
        store.finish_scan(self.scan(), error='inference failed', processed_records=[changed])
        self.assertEqual(store.unprocessed_records(key, [changed]), ['1'])

    def test_repeated_fact_is_updated_with_source_time_without_becoming_task(self):
        pid = self.save(profile(1))[0]
        p = self.store.profiles()[0]
        self.assertEqual(p['observed_at'], NOW)
        ids = self.save(profile(2, profile_id=pid), records=[record(2, at=NOW+10)])
        self.assertEqual(ids, [pid])
        self.assertEqual(len(self.store.profiles()), 1)
        self.assertEqual(self.store.profiles()[0]['observed_at'], NOW+10)
        self.assertEqual(len(self.store.evidence('profile', pid)), 2)
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.events(), [])

    def test_future_replacement_then_retraction_share_one_current_reader(self):
        pid = self.save(profile(1, '住在旧城区', category='address', field='residence_area'))[0]
        start = '2027-01-16T08:00:00+08:00'
        boundary = 1800057600
        replacement = profile(2, '住在新区', category='address', field='residence_area',
            profile_id=pid, operation='replace', valid_from=start, time_text='1月16日')
        new_id = self.save(replacement, records=[record(2, '1月16日搬到新区', at=NOW+10)])[0]
        for now, expected in [(NOW+100, pid), (boundary, new_id)]:
            self.assertEqual([p['id'] for p in self.store.profiles(now=now)], [expected])
            reader = open_assistant_knowledge(self.path, now=now)
            self.assertEqual([int(f.ref.record_id) for f in reader.facts()], [expected])
            memory = PersonalContext(self.store).snapshot(now)['memories']
            self.assertEqual([m['profile_id'] for m in memory], [expected])
        self.save(profile(3, '住在新区', category='address', field='residence_area',
            profile_id=new_id, operation='retract'), records=[record(3, '不住新区了', at=boundary+10)])
        self.assertEqual(self.store.profiles(now=boundary+20), [])
        self.assertEqual(open_assistant_knowledge(self.path, now=boundary+20).facts(), ())

    def test_temporary_expiry_and_unknown_relative_time_are_not_durable(self):
        ids = self.save(profile(1, '暂住某地', category='address', field='other_location',
            temporal_kind='temporary', valid_until='2027-01-16T08:00:00+08:00', time_text='1月16日'),
            records=[record(1, '暂住到1月16日')])
        self.assertEqual(len(self.store.profiles(now=NOW)), 1)
        self.assertEqual(self.store.profiles(now=1800057600), [])
        self.assertEqual(self.save(profile(2, temporal_kind='temporary', valid_until='+1d', time_text='明天'),
            records=[record(2, '明天')]), [])
        self.assertEqual(len(self.store.profiles(include_dismissed=True)), len(ids))

    def test_dismissal_older_observation_and_other_subject_cannot_replace(self):
        pid = self.save(profile(1))[0]
        self.assertEqual(self.save(profile(2, '喜欢音乐', operation='replace', profile_id=pid),
                                  records=[record(2, at=NOW-1)]), [])
        self.assertEqual(self.save(profile(3, '喜欢音乐', operation='replace', profile_id=pid),
                                  records=[record(3, is_self=False)]), [])
        self.store.dismiss_profile(pid)
        self.assertEqual(self.save(profile(4), records=[record(4, at=NOW+10)]), [])
        self.assertEqual(self.store.profiles(), [])

    def test_inference_cannot_rewrite_an_explicit_memory(self):
        pid = self.save(profile(1))[0]
        self.save(profile(2, confidence='inferred', temporal_kind='historical'), records=[record(2, at=NOW+10)])
        memory = self.store.profiles(now=NOW+20)[0]
        self.assertEqual((memory['id'], memory['observed_at'], memory['temporal_kind']), (pid, NOW, 'durable'))
        self.assertEqual(memory['evidence'][0]['id'], '1')

    def test_concurrent_memory_edit_takes_precedence(self):
        pid = self.save(profile(1))[0]
        msg = record(2, '现在喜欢摄影', at=NOW+10)
        output = dict(summary='', tasks=[], events=[], profiles=[profile(2, '喜欢摄影',
            operation='replace', profile_id=pid)], insights=[])
        _, _, facts, _ = ExtractionWorker.candidates(SCOPE, [msg], output,
            self.store.profiles(include_dismissed=True), ['2'])
        self.store.dismiss_profile(pid)
        self.assertEqual(self.store.finish_scan(self.scan(), profiles=facts)['profile_ids'], [])
        self.assertEqual(self.store.profiles(), [])

    def test_full_incremental_extraction_retries_failure_and_ignores_replayed_input(self):
        batch, prompts = [record(1)], []
        outputs = [RuntimeError('failed'), dict(summary='', tasks=[], events=[], insights=[], profiles=[profile(1)]),
            dict(summary='', tasks=[], events=[], insights=[], profiles=[profile(2, '喜欢摄影')])]

        class Runtime:
            async def create_thread(self):
                pass

            async def run_turn(self, prompt, **kwargs):
                prompts.append(json.loads(prompt))
                value = outputs.pop(0)
                if isinstance(value, Exception):
                    raise value
                return SimpleNamespace(status='completed', final_message=json.dumps(value))

            async def close(self):
                pass

        worker = ExtractionWorker(self.store, source_loader=lambda *_: RecordBatch(tuple(batch)), runtime_factory=Runtime)
        asyncio.run(worker.extract(self.scan(), Event()))
        self.assertEqual(self.store.unprocessed_records(source_key(SCOPE), batch), ['1'])
        asyncio.run(worker.extract(self.scan(), Event()))
        asyncio.run(worker.extract(self.scan(), Event()))
        self.assertEqual(len(prompts), 2)
        batch[:] = [record(1), record(2, at=NOW+10)]
        asyncio.run(worker.extract(self.scan(), Event()))
        self.assertEqual(prompts[-1]['new_record_ids'], ['2'])
        self.assertEqual(len(self.store.profiles()), 2)

    @unittest.skipUnless(os.environ.get('PERSONAL_ASSISTANT_LIVE_TEST'), 'opt-in model regression')
    def test_live_explicit_preference_change_updates_memory_without_creating_work(self):
        from personal_assistant.cli import DEFAULT_DB
        from personal_assistant.extraction import INSTRUCTIONS
        pid = self.save(profile(1, '喜欢辣菜', category='preference', field='food_preference'))[0]
        messages = [record(1, '我喜欢辣菜'), record(2, '我现在完全不吃辣了，以后都只吃不辣的。', at=NOW+10)]
        worker = ExtractionWorker(self.store, source_loader=lambda *_: RecordBatch(tuple(messages)),
            runtime_factory=lambda: build_runtime(SimpleNamespace(path=DEFAULT_DB), INSTRUCTIONS, model='gpt-6-luna'))
        asyncio.run(asyncio.wait_for(worker.extract(self.scan(), Event()), timeout=90))
        self.assertEqual(self.store.scans()[0]['status'], 'completed')
        current = self.store.profiles(now=NOW+20)
        self.assertTrue(current)
        self.assertNotIn(pid, [p['id'] for p in current])
        self.assertTrue(any('不辣' in p['value'] or '不吃辣' in p['value'] for p in current))
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.events(), [])

    def test_dh_page_ids_validity_and_current_location_resolution(self):
        def page(page_id, value, field='interest', **extra):
            return dict(id=page_id, knowledge=dict(schema='wiki_structured_v1',
                subject=dict(self_account='self'), entities=[dict(id='me', kind='person', label='我',
                    account='self', identity_status='exact_account')], attributes=[dict(id='a1', subject_id='me',
                    mode='self_report', polarity='affirmed', field=field, value=value,
                    last_observed_at=NOW-100, **extra)]))

        path = Path(self.directory.name)/'knowledge.json'
        path.write_text(json.dumps(dict(schema='source_bound_wiki_library_v1', pages=[
            page('one', '喜欢摄影'), page('two', '喜欢建筑'),
            page('old', '旧城区', field='residence_area'),
            page('expired', '过期偏好', valid_to='2026-01-01'),
            page('historical', '多年前的喜好', temporal_kind='historical') ])))
        self.store.set_settings(dict(self.store.settings(), dh_knowledge=[str(path)]))
        self.save(profile(1, '新城区', category='address', field='residence_area'))
        context = PersonalContext(self.store).snapshot(NOW)
        values = {m['value'] for m in context['memories']}
        self.assertEqual(values, {'喜欢摄影', '喜欢建筑', '新城区', '多年前的喜好'})
        self.assertEqual(len({f['id'] for f in context['dh_facts']}), 3)
        self.assertEqual(context['historical_locations'][0]['value'], '新城区')
        self.assertIsNone(context['current_location'])
        _, _, prompt, _ = ProactiveWorker(self.store).prepare(NOW)
        public = prompt['personal_context']
        self.assertNotIn('chat_profiles', public)
        self.assertNotIn('dh_facts', public)
        self.assertNotIn('evidence', json.dumps(public))
        self.assertEqual(len(public['memories']), 4)

    def test_migration_preserves_source_time_and_previous_processing(self):
        path = Path(self.directory.name)/'legacy.db'
        original = record(1)
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE profile_facts (
                id INTEGER PRIMARY KEY, account_key TEXT, subject_key TEXT, subject_name TEXT,
                identity_basis TEXT, is_self INTEGER, category TEXT, value TEXT, confidence TEXT,
                evidence_json TEXT, status TEXT DEFAULT 'active', created_at REAL,
                UNIQUE(subject_key,category,value));
                CREATE TABLE history_scan_state (source_key TEXT PRIMARY KEY,signature TEXT NOT NULL);''')
            db.execute('INSERT INTO profile_facts VALUES (1,?,?,?,?,?,?,?,?,?,?,?)',
                ('account', 'subject', '我', 'source_id', 1, 'interest', '看展', 'explicit',
                 json.dumps([asdict(original)]), 'active', NOW+1000))
            db.execute('INSERT INTO history_scan_state VALUES (?,?)',
                       (source_key(SCOPE), json.dumps({'1': original.content_hash})))
        store = TaskStore(path)
        self.assertEqual(store.profiles()[0]['observed_at'], NOW)
        self.assertEqual(store.unprocessed_records(source_key(SCOPE), [original]), [])
        self.assertEqual(open_assistant_knowledge(path).facts()[0].fields['observed_at'], NOW)


if __name__ == '__main__':
    unittest.main()
