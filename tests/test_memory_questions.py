"""Read-only recall, durable answers and the existing private mobile boundary."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from app import Assistant
from common.history import (HistoryChat, HistoryCursor, HistoryMessage, HistoryPage,
                            MessageRef, MessageTime, SenderEvidence, SourceBinding)
from memory.qa import MemoryQuestions, Recall
from memory.store import TaskStore
from personal_context.knowledge import KnowledgeEntity, KnowledgeReader, KnowledgeRef
from presentation.task_web import task_server


def at(day, hour=0):
    return datetime(2026, 11, day, hour, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class QuestionsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(Path(directory.name) / 'assistant.db')
        self.store.set_settings(dict(self.store.settings(), dh_knowledge=[], rpa_memory=[],
            history_db='/bound/history.db', history_account='self', history_source='sample'))
        self.questions = MemoryQuestions(self.store)

    def test_queue_deduplicates_concurrent_requests_and_survives_restart(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            asked = list(pool.map(lambda _: self.questions.ask({'question': '我答应了什么？'}), range(2)))
        self.assertEqual(asked[0]['id'], asked[1]['id'])
        first = self.questions.claim()
        self.assertIsNone(self.questions.claim())
        with self.assertRaises(ValueError):
            self.questions.ask(dict(question='什么时候？', parent_id=first['id']))
        restarted = MemoryQuestions(TaskStore(self.store.path))
        restarted.recover()
        self.assertEqual(restarted.claim()['id'], first['id'])
        self.assertEqual(restarted.get(first['id'])['status'], 'running')
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.events(), [])
        for question in ('', 'x' * 1001):
            with self.assertRaises(ValueError):
                self.questions.ask(dict(question=question))

    def test_history_is_bound_paged_and_not_repeated(self):
        source = SourceBinding('sample', 'self')
        message = HistoryMessage(MessageRef(source, 'chat', 'm1'),
            SenderEvidence('peer', '小陈', 'source_id', False), MessageTime(at(6), at(6), None),
            '改到周日，之前周六的约定取消', 'text', None, False, '/private/raw.txt', 'hash')
        reader = Mock()
        reader.query.return_value = HistoryPage(source, (message,), HistoryCursor(at(6), 8), at(8))
        reader.list_chats.return_value = (
            HistoryChat(source, 'one', '小陈', 'direct', at(6)),
            HistoryChat(source, 'two', '小陈', 'direct', at(7)))
        recall = Recall(self.store)
        tools = {t.name: t.handler for t in recall.tools()}
        args = dict(contains='吃饭', start='2026-11-06', end='2026-11-08')
        with patch('memory.qa.history_reader', return_value=reader) as opener:
            result = tools['read_messages'](args)
            self.assertTrue(tools['read_messages'](args)['already_read'])
            cursor = result['next_cursor']
            self.assertEqual(cursor, dict(recorded_at=at(6), row_id=8))
            tools['read_messages'](dict(args, cursor=cursor))
            chats = tools['find_chats'](dict(name='小陈'))['chats']
            self.assertEqual([c['chat_id'] for c in chats], ['one', 'two'])
            opener.assert_called_once_with(recall.sources)
        self.assertEqual(reader.query.call_count, 2)
        query = reader.query.call_args.args[0]
        self.assertEqual(query.start_recorded_at, at(6))
        self.assertEqual(query.end_recorded_at, at(8))
        self.assertEqual(query.contains, '吃饭')
        self.assertEqual(reader.query.call_args.kwargs['cursor'], HistoryCursor(at(6), 8))
        self.assertEqual(result['messages'][0]['time']['recorded_at'], '2026-11-06T00:00:00+08:00')
        self.assertNotIn('/private', json.dumps(result))
        self.assertFalse(result['messages'][0]['sender']['is_self'])

    def test_memory_preserves_history_and_identity_and_loads_knowledge_once(self):
        with self.store.connect() as db:
            for status, value in [('active', '喜欢清淡'), ('superseded', '以前喜欢辣'),
                                  ('dismissed', '误记'), ('retracted', '已撤销')]:
                db.execute('INSERT INTO profile_facts (account_key,subject_key,subject_name,identity_basis,'
                    'is_self,category,value,confidence,evidence_json,status,created_at,valid_until) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('self', 'person:self', '本人', 'source_id', 1, 'preference', value,
                     'explicit', '[]', status, at(6), at(7)))
        sources = dict(self.store.settings(), dh_knowledge=['/bound/knowledge'])
        self.store.set_settings(sources)
        binding = SourceBinding('dh', 'self')
        entities = [KnowledgeEntity(KnowledgeRef(binding, 'people', key), 'person', '小陈',
                    key, 'exact_account', {}) for key in ('one', 'two')]
        reader = KnowledgeReader(entities=entities)
        recall = Recall(self.store)
        with patch('memory.qa.open_dh_knowledge', return_value=reader) as opener:
            result = recall.query('memories', recall.memories)(dict(query=''))
            again = recall.query('memories', recall.memories)(dict(query='小陈'))
            opener.assert_called_once_with('/bound/knowledge')
        self.assertEqual({p['status'] for p in result['profiles']}, {'active', 'superseded'})
        self.assertEqual(result['profiles'][0]['valid_until'], '2026-11-07T00:00:00+08:00')
        self.assertEqual({p['account'] for p in again['people']}, {'one', 'two'})

    def test_plans_include_cancellations_and_uncertain_recurrence(self):
        self.store.create_event(title='吃饭', starts_at=at(6, 12), ends_at=at(6, 13),
            recurrence=dict(frequency='weekly', weekdays=[4], weekly_count=2))
        self.store.create_event(title='吃饭取消', scheduled_date='2026-11-07', status='cancelled')
        task = self.store.create(description='吃饭订位', due_at=at(7, 10))
        self.store.update(task['id'], dict(status='done'))
        self.store.create(description='吃饭未定日期')
        recall = Recall(self.store)
        result = recall.plans(dict(query='吃饭', start='2026-11-07', end='2026-11-08'))
        self.assertEqual(len(result['events']), 2)
        self.assertIn('cancelled', [e['status'] for e in result['events']])
        recurring = next(e for e in result['events'] if e['status'] != 'cancelled')
        self.assertTrue(recurring['uncertain'])
        self.assertEqual(recurring['scheduled_date'], '2026-11-07')
        self.assertEqual([t['status'] for t in result['tasks']], ['done'])
        self.assertEqual(len(self.store.tasks()), 2)

    def test_context_budget_reports_omissions_and_unavailable_is_not_absence(self):
        recall = Recall(self.store)
        result = recall.query('large', lambda _: dict(records=[dict(text='x' * 23000), dict(text='ok')]))({})
        self.assertEqual(result['records'], [dict(text='ok')])
        self.assertEqual(result['omitted'], ['records'])
        result = recall.query('broken', Mock(side_effect=OSError('/private/secret/path')))({})
        self.assertTrue(result['unavailable'])
        self.assertNotIn('/private', json.dumps(result))

    def test_answer_uses_tools_and_followup_then_persists_without_creating_tasks(self):
        prompts, closed = [], []

        class Runtime:
            def __init__(self, recall):
                self.tools = {t.name: t.handler for t in recall.tools()}

            async def create_thread(self):
                pass

            async def run_turn(self, prompt, *, output_schema):
                prompts.append(json.loads(prompt))
                plans = self.tools['search_plans'](dict(query=''))
                assert plans['tasks'] == []
                return SimpleNamespace(status='completed', final_message=json.dumps(dict(answer='现有资料不足，无法确认约定。')))

            async def close(self):
                closed.append(True)

        questions = MemoryQuestions(self.store, runtime_factory=Runtime)
        asked = questions.ask(dict(question='我答应了什么？'))
        asyncio.run(questions.answer(questions.claim(), Event()))
        saved = MemoryQuestions(TaskStore(self.store.path)).get(asked['id'])
        self.assertEqual(saved['status'], 'completed')
        self.assertIn('不足', saved['answer'])
        questions.ask(dict(question='还有呢？', parent_id=asked['id']))
        asyncio.run(questions.answer(questions.claim(), Event()))
        self.assertEqual(prompts[-1]['previous'], [dict(question=asked['question'], answer=saved['answer'])])
        self.assertEqual(len(closed), 2)
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.events(), [])

    def test_failure_retry_and_service_stop_keep_a_recoverable_question(self):
        class Runtime:
            async def create_thread(self):
                raise RuntimeError('/private/model-error')

            async def close(self):
                pass

        questions = MemoryQuestions(self.store, runtime_factory=lambda _: Runtime())
        item = questions.ask(dict(question='我的偏好？'))
        with self.assertLogs('memory.qa', level='ERROR'):
            asyncio.run(questions.answer(questions.claim(), Event()))
        saved = questions.get(item['id'])
        self.assertEqual(saved['status'], 'failed')
        self.assertNotIn('/private', saved['error'])
        self.assertEqual(questions.retry(item['id'])['status'], 'queued')
        claimed = questions.claim()
        with patch('memory.qa.infer_json', side_effect=InterruptedError):
            asyncio.run(questions.answer(claimed, Event()))
        self.assertEqual(questions.get(item['id'])['status'], 'queued')

    def test_mobile_questions_require_calendar_token_and_same_origin(self):
        app = Assistant(self.store.path)
        server = task_server(app, 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        path = '/calendar/' + self.store.calendar_token()

        def request(method, target, data=None, origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            connection.request(method, target, json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            return response.status, response.read().decode()

        self.assertEqual(request('GET', '/api/questions')[0], 404)
        self.assertEqual(request('GET', '/calendar/' + 'x' * 43 + '/questions')[0], 404)
        task = self.store.create(description='普通任务')
        self.assertEqual(request('GET', '/calendar/' + self.store.task_token(task['id']) + '/questions')[0], 404)
        self.assertEqual(request('POST', path + '/questions', dict(question='我有何偏好？'),
                                 'https://other.example')[0], 403)
        code, body = request('POST', path + '/questions', dict(question='<img onerror=alert(1)>'))
        self.assertEqual(code, 202)
        asked = json.loads(body)
        code, body = request('GET', path + '/questions')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)[0]['id'], asked['id'])
        page = request('GET', path)[1]
        self.assertNotIn('问问记忆', page)
        self.assertNotIn('questions.js', page)
        self.assertNotIn('questions.css', page)
        self.assertNotIn('questions-panel', page)
        self.assertEqual(request('GET', '/assets/questions.js')[0], 200)


if __name__ == '__main__':
    unittest.main()
