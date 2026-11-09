"""Input handling shared by the small local UI and CLI."""

from dataclasses import asdict
from pathlib import Path
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from personal_context.wechat_push import WeChatPush
from followup import FollowUp
from inputs.wechat import SOURCE_ERRORS, history_reader, message_ids
from memory.store import TaskStore
from memory.qa import MemoryQuestions
from execution import ExecutionWorker
from memory.extraction import ExtractionWorker, enqueue
from inputs.devices import DeviceInputs
from proactive import ProactiveWorker
from common.fields import timestamp, task_fields, event_fields


class Assistant:
    def __init__(self, db_path, *, notify=False, wechat=False, wechat_credentials=None, public_url=None):
        if public_url:
            parsed = urlparse(public_url)
            if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                raise ValueError('手机入口请填写 HTTPS 根地址，例如 https://assistant.example.com')
            public_url = public_url.rstrip('/')
        self.store = TaskStore(db_path)
        self.devices = DeviceInputs(self.store)
        self.questions = MemoryQuestions(self.store)
        self.followup = FollowUp(self.store, notify=notify,
                                 wechat=WeChatPush(wechat_credentials) if wechat else None,
                                 public_url=public_url)
        self.execution = ExecutionWorker(self.store, followup=self.followup)
        self.extraction = ExtractionWorker(self.store, wechat=self.followup.wechat)
        self.proactive = ProactiveWorker(self.store, wechat=self.followup.wechat)

    def personalize(self, data):
        value = self.store.personalization()
        if 'enabled' in data:
            value['enabled'] = bool(data['enabled'])
        if 'location' in data:
            position = data['location']
            if position is None:
                value.pop('current_location', None)
            else:
                latitude, longitude = float(position['latitude']), float(position['longitude'])
                if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                    raise ValueError('定位结果无效，请重试')
                value['current_location'] = dict(latitude=round(latitude, 2), longitude=round(longitude, 2),
                    observed_at=time.time(), source='device_location')
        self.store.set_personalization(value)
        return value

    def extract(self, data):
        return enqueue(self.store, data)

    def create(self, data):
        sources = self.store.settings()
        fields = task_fields(data, sources['timezone'])
        if not fields.get('description'):
            raise ValueError('请填写待办内容')
        fields.pop('status', None)
        scope = dict(sources, chat_id=data.get('chat_id') or None,
                     assistant_knowledge=str(self.store.path),
                     knowledge_query=data.get('knowledge_query') or None,
                     person_account=data.get('person_account') or None)
        observed, warnings = None, []
        if scope['chat_id']:
            try:
                reader = history_reader(scope)
                chat = reader.get_chat(scope['chat_id'])
                scope['history_source'] = chat.source.source_id
                scope['chat_name'] = chat.display_name
                observed = message_ids(reader.recent(scope['chat_id'], limit=30))
            except SOURCE_ERRORS as exc:
                warnings.append(str(exc))
        task = self.store.create(**fields, timezone=sources['timezone'], scope=scope, observed=observed)
        if data.get('execute'):
            self.store.enqueue_execution(task['id'], data.get('instruction') or task['description'])
        return dict(task=task, source_warnings=warnings)

    def update(self, task_id, data):
        task = self.store.get(task_id)
        if 'deleted' in data:
            if not isinstance(data['deleted'], bool):
                raise ValueError('删除状态须为布尔值')
            return self.store.set_deleted('task', task_id, data['deleted'])
        if data.get('execution_action') == 'start':
            self.store.enqueue_execution(task_id, data.get('instruction', ''))
            return task
        if data.get('execution_action') == 'stop':
            self.store.stop_execution(task_id)
            return task
        return self.store.update(task_id, task_fields(data, task['timezone']))

    def create_event(self, data):
        timezone = self.store.settings()['timezone']
        fields = event_fields(data, timezone)
        if not fields.get('title'):
            raise ValueError('请填写日程标题')
        return self.store.create_event(**fields, timezone=timezone)

    def update_event(self, event_id, data):
        event = self.store.event(event_id)
        if event['scope'].get('source_kind') == 'apple_calendar':
            raise ValueError('这是 Apple 日历导入的日程，请在 Apple 日历中修改或取消，助理会自动更新')
        if 'deleted' in data:
            if not isinstance(data['deleted'], bool):
                raise ValueError('删除状态须为布尔值')
            return self.store.set_deleted('event', event_id, data['deleted'])
        return self.store.update_event(event_id, event_fields(data, event['timezone'], event))

    def chats(self, query=None):
        return [asdict(chat) for chat in history_reader(self.store.settings()).list_chats(name=query, limit=50)]

    def set_sources(self, data):
        sources = self.store.settings()
        for key in ('history_db', 'history_account', 'history_source', 'timezone',
                    'dh_knowledge', 'rpa_memory'):
            if key in data:
                sources[key] = data[key]
        ZoneInfo(sources['timezone'])
        sources['history_db'] = str(Path(sources['history_db']).expanduser().resolve())
        for key in ('dh_knowledge', 'rpa_memory'):
            sources[key] = [str(Path(path).expanduser().resolve()) for path in sources[key]]
        self.store.set_settings(sources)
        return sources
