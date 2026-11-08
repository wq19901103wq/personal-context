"""Exercise the stdio boundary without a provider or private data."""

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from common.codex_runtime import CodexRuntime, DynamicTool


SERVER = '''
import json, sys
def send(value):
    print(json.dumps(value), flush=True)
def notice(method, **params):
    send(dict(method=method, params=dict(threadId='thread-1', **params)))
for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get('method'), message.get('params', {})
    if method == 'initialize':
        send(dict(id=message['id'], result={}))
    elif method in ('thread/start', 'thread/resume'):
        assert params['approvalPolicy'] == 'never'
        assert params['sandbox'] == 'read-only'
        if method == 'thread/start':
            assert params['environments'] == []
        else:
            assert params['threadId'] == 'thread-1' and params['excludeTurns']
        send(dict(id=message['id'], result=dict(thread=dict(id='thread-1'))))
    elif method == 'turn/start':
        assert params['environments'] == []
        if MODE == 'eof':
            sys.exit(0)
        if MODE == 'rpc_error':
            send(dict(id=message['id'], error=dict(code=-1, message='start failed')))
            continue
        notice('turn/started', turn=dict(id='turn-1', status='inProgress'))
        if MODE in ('tool', 'tool_error'):
            send(dict(id='tool-1', method='item/tool/call', params=dict(
                threadId='thread-1', turnId='turn-1', tool='lookup', arguments=dict(key='demo'))))
        elif MODE == 'interrupt':
            send(dict(id=message['id'], result=dict(turn=dict(id='turn-1'))))
        else:
            assert params['outputSchema']['type'] == 'object'
            notice('item/completed', turnId='turn-1', item=dict(
                type='agentMessage', phase='final_answer', text='{"ok":true}'))
            notice('turn/completed', turn=dict(id='turn-1', status='completed'))
            # Completion before the RPC response must not leave an active turn.
            send(dict(id=message['id'], result=dict(turn=dict(id='turn-1'))))
        if MODE in ('tool', 'tool_error'):
            start_id = message['id']
    elif method == 'turn/interrupt':
        assert params['turnId'] == 'turn-1'
        send(dict(id=message['id'], result={}))
        notice('turn/completed', turn=dict(id='turn-1', status='interrupted'))
    elif message.get('id') == 'tool-1':
        result = message['result']
        assert result['success'] == (MODE == 'tool')
        notice('item/completed', turnId='turn-1', item=dict(
            type='agentMessage', phase='final_answer', text=result['contentItems'][0]['text']))
        notice('turn/completed', turn=dict(id='turn-1', status='completed'))
        send(dict(id=start_id, result=dict(turn=dict(id='turn-1'))))
'''


class CodexRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'login')})
        environment.start()
        self.addCleanup(environment.stop)

    def runtime(self, mode, **kwargs):
        binary = self.root / 'fake-codex'
        binary.write_text(f'#!{sys.executable}\nMODE = {mode!r}\n' + SERVER)
        binary.chmod(0o700)
        runtime = CodexRuntime(codex_bin=str(binary), cwd=self.root / 'workspace',
                               codex_home=self.root / 'state', developer_instructions='test',
                               thread_config={}, **kwargs)
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def test_structured_completion_before_start_response(self):
        runtime = self.runtime('early')
        result = await asyncio.wait_for(runtime.run_turn('test', output_schema={'type': 'object'}), 5)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(json.loads(result.final_message), {'ok': True})
        self.assertIsNone(runtime.active_turn_id)

    async def test_dynamic_tool_before_start_response_and_handler_error(self):
        for mode in ('tool', 'tool_error'):
            calls = []
            def lookup(args):
                calls.append(args)
                if mode == 'tool_error':
                    raise ValueError('lookup failed')
                return {'value': 'demo'}
            runtime = self.runtime(mode, tools=[DynamicTool('lookup', 'test', {}, lookup)])
            result = await asyncio.wait_for(runtime.run_turn('lookup'), 5)
            self.assertEqual(calls, [{'key': 'demo'}])
            self.assertEqual(result.final_message, 'lookup failed' if mode == 'tool_error'
                             else '{"value": "demo"}')
            await runtime.close()

    async def test_resume_and_interrupt(self):
        runtime = self.runtime('interrupt')
        self.assertEqual(await runtime.resume_thread('thread-1'), 'thread-1')
        turn = asyncio.create_task(runtime.run_turn('wait'))
        async def interrupt():
            while runtime.active_turn_id is None:
                await asyncio.sleep(.01)
            await runtime.interrupt()
            self.assertEqual((await turn).status, 'interrupted')
        await asyncio.wait_for(interrupt(), 5)

    async def test_eof_and_rpc_errors_unblock_turn(self):
        for mode, error in (('eof', '已退出'), ('rpc_error', 'start failed')):
            runtime = self.runtime(mode)
            with self.assertRaisesRegex(RuntimeError, error):
                await asyncio.wait_for(runtime.run_turn('test'), 5)
            await runtime.close()

    async def test_public_event_handler_error_propagates(self):
        def fail(event):
            raise ValueError('progress failed')
        runtime = self.runtime('early', event_handler=fail)
        with self.assertRaisesRegex(ValueError, 'progress failed'):
            await asyncio.wait_for(runtime.run_turn('test', output_schema={'type': 'object'}), 5)


@unittest.skipUnless(os.environ.get('PERSONAL_ASSISTANT_LIVE_TEST'), 'opt-in Codex smoke')
class LiveCodexRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_structured_tool_call_and_persisted_thread(self):
        from execution import build_runtime
        import secrets
        with TemporaryDirectory() as directory:
            store = SimpleNamespace(path=Path(directory) / 'assistant.db')
            value, calls = secrets.randbelow(1000000), []
            def lookup(args):
                calls.append(args)
                return {'value': value}
            tool = DynamicTool('lookup', 'Read the test value.', dict(type='object',
                properties=dict(key=dict(type='string')), required=['key'], additionalProperties=False), lookup)
            schema = dict(type='object', properties=dict(value=dict(type='integer')),
                          required=['value'], additionalProperties=False)
            thread_id = None
            for _ in range(2):
                runtime = build_runtime(store, 'Use lookup to obtain the value. Return only the requested JSON.',
                                        tools=[tool], model='gpt-6-luna')
                try:
                    if thread_id:
                        self.assertEqual(await runtime.resume_thread(thread_id), thread_id)
                    else:
                        thread_id = await runtime.create_thread()
                    result = await asyncio.wait_for(runtime.run_turn(
                        'Call lookup once with key demo now, then return its value.', output_schema=schema), 90)
                    self.assertEqual(result.status, 'completed')
                    self.assertEqual(json.loads(result.final_message), {'value': value})
                finally:
                    await runtime.close()
            self.assertEqual(calls, [{'key': 'demo'}, {'key': 'demo'}])


if __name__ == '__main__':
    unittest.main()
