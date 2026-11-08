"""Small stdio client; Codex owns the model, tool loop and persisted threads.

Protocol reference: openai/codex rust-v0.144.1 (44918ea10c0f),
app-server-protocol/src/protocol/v2/{thread,turn}.rs and protocol/dynamic_tools.rs.
"""

import asyncio
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import signal
from typing import Callable


@dataclass(frozen=True)
class DynamicTool:
    name: str
    description: str
    input_schema: dict
    handler: Callable

    def codex_spec(self):
        return dict(type='function', name=self.name, description=self.description,
                    inputSchema=self.input_schema, deferLoading=False)


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    payload: dict


@dataclass(frozen=True)
class TurnResult:
    status: str
    final_message: str | None
    events: tuple


class CodexRuntime:
    def __init__(self, *, codex_bin, cwd, codex_home, developer_instructions,
                 thread_config, tools=(), event_handler=None):
        self.codex_bin, self.cwd, self.codex_home = codex_bin, Path(cwd), Path(codex_home)
        self.instructions, self.config = developer_instructions, thread_config
        self.tools = {tool.name: tool for tool in tools}
        self.event_handler = event_handler
        self.thread_id = self.active_turn_id = None
        self._process = None
        self._reader = self._stderr = None
        self._requests, self._tool_jobs = {}, set()
        self._messages = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._next_id, self._failure = 0, None
        self._completed_turn = None

    async def start(self):
        if self._process:
            return
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.codex_home.mkdir(parents=True, exist_ok=True)
        # Share only the login, never global MCP servers, skills or config.
        auth = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'auth.json'
        target = self.codex_home / 'auth.json'
        if auth.exists() and not target.is_symlink() and not target.exists():
            target.symlink_to(auth.resolve())
        environment = dict(os.environ, CODEX_HOME=str(self.codex_home.resolve()))
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.codex_bin, 'app-server', cwd=str(self.cwd), env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
                limit=16 * 1024 * 1024)
        except FileNotFoundError as exc:
            raise RuntimeError('请安装 Codex CLI 0.144.1 并运行 codex login，或设置 CODEX_BIN') from exc
        self._reader = asyncio.create_task(self._read_messages())
        self._stderr = asyncio.create_task(self._drain_stderr())
        try:
            await self._request('initialize', dict(
                clientInfo=dict(name='personal-context', title='Personal Context', version='0.1.0'),
                capabilities=dict(experimentalApi=True)))
            await self._send(dict(method='initialized', params={}))
        except BaseException:
            await self.close()
            raise

    async def _send(self, message):
        async with self._write_lock:
            if self._failure:
                raise self._failure
            self._process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
            await self._process.stdin.drain()

    async def _request(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._requests[request_id] = future
        try:
            await self._send(dict(id=request_id, method=method, params=params))
            return await asyncio.wait_for(future, timeout=60)
        finally:
            self._requests.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Also consume failure if writing the request failed first.

    def _fail(self, error):
        if self._failure:
            return
        self._failure = error
        for future in self._requests.values():
            if not future.done():
                future.set_exception(error)
        self._messages.put_nowait(error)
        self.active_turn_id = None

    async def _read_messages(self):
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                if 'method' not in message:
                    future = self._requests.get(message.get('id'))
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(RuntimeError(message['error']['message']))
                        else:
                            future.set_result(message['result'])
                elif 'id' in message:
                    job = asyncio.create_task(self._answer_tool(message))
                    self._tool_jobs.add(job)
                    job.add_done_callback(self._tool_jobs.discard)
                else:
                    params = message.get('params', {})
                    if params.get('threadId') == self.thread_id:
                        turn_id = params.get('turn', {}).get('id')
                        if message['method'] == 'turn/started':
                            self.active_turn_id = turn_id
                        elif message['method'] == 'turn/completed':
                            self._completed_turn = turn_id
                            if turn_id == self.active_turn_id:
                                self.active_turn_id = None
                    self._messages.put_nowait(message)
            self._fail(RuntimeError('Codex app-server 已退出'))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(RuntimeError(f'Codex app-server 连接中断：{type(exc).__name__}'))

    async def _drain_stderr(self):
        # Keep the pipe flowing without retaining provider diagnostics or secrets.
        while await self._process.stderr.read(8192):
            pass

    async def _answer_tool(self, message):
        try:
            params = message.get('params', {})
            if message['method'] != 'item/tool/call':
                await self._send(dict(id=message['id'], error=dict(
                    code=-32601, message='This assistant does not support this request')))
                return
            tool = self.tools.get(params.get('tool'))
            try:
                if (not tool or params.get('threadId') != self.thread_id or
                        params.get('turnId') != self.active_turn_id):
                    raise ValueError('Tool is not available in this task and turn')
                result = tool.handler(params['arguments'])
                if inspect.isawaitable(result):
                    result = await result
                content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                success = True
            except Exception as exc:
                content, success = str(exc), False
            await self._send(dict(id=message['id'], result=dict(
                contentItems=[dict(type='inputText', text=content)], success=success)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(RuntimeError(f'Codex 工具响应失败：{type(exc).__name__}'))

    def _thread_params(self):
        return dict(cwd=str(self.cwd.resolve()), sandbox='read-only', approvalPolicy='never',
                    developerInstructions=self.instructions, config=self.config)

    async def create_thread(self):
        await self.start()
        response = await self._request('thread/start', dict(self._thread_params(),
            dynamicTools=[t.codex_spec() for t in self.tools.values()], environments=[]))
        self.thread_id = response['thread']['id']
        return self.thread_id

    async def resume_thread(self, thread_id):
        await self.start()
        response = await self._request('thread/resume', dict(
            self._thread_params(), threadId=thread_id, excludeTurns=True))
        self.thread_id = response['thread']['id']
        return self.thread_id

    async def run_turn(self, text, *, output_schema=None):
        async with self._turn_lock:
            if not self.thread_id:
                await self.create_thread()
            params = dict(threadId=self.thread_id, environments=[],
                          input=[dict(type='text', text=text, textElements=[])])
            if output_schema is not None:
                params['outputSchema'] = output_schema
            response = await self._request('turn/start', params)
            turn_id = response['turn']['id']
            if self._completed_turn != turn_id:
                self.active_turn_id = turn_id
            events, final = [], None
            while True:
                message = await self._messages.get()
                if isinstance(message, Exception):
                    raise message
                payload, kind = message.get('params', {}), message['method']
                if payload.get('threadId') != self.thread_id:
                    continue
                event_turn = payload.get('turnId') or payload.get('turn', {}).get('id')
                if event_turn and event_turn != turn_id:
                    continue
                event = AgentEvent(kind, payload)
                # Consumers persist only public progress, never reasoning events.
                if kind in ('item/completed', 'turn/plan/updated', 'turn/completed'):
                    if self.event_handler:
                        result = self.event_handler(event)
                        if inspect.isawaitable(result):
                            await result
                if kind == 'item/completed':
                    item = payload['item']
                    if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                        final = item.get('text')
                elif kind == 'turn/completed':
                    events.append(event)
                    self.active_turn_id = None
                    return TurnResult(payload['turn']['status'], final, tuple(events))

    async def interrupt(self):
        if self.thread_id and self.active_turn_id:
            await self._request('turn/interrupt', dict(
                threadId=self.thread_id, turnId=self.active_turn_id))

    async def close(self):
        process = self._process
        if not process:
            return
        self._fail(RuntimeError('Codex app-server 已关闭'))
        jobs = [j for j in (self._reader, self._stderr, *self._tool_jobs) if j]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        self._process = None
