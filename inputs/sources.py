"""Connectors read original data; they do not infer tasks or decide notifications."""

import time

from common.history import HistoryQuery
from common.records import RawRecord, RecordBatch
from .wechat import history_reader
from .devices import device_records


def wechat_records(scope, hours):
    page = history_reader(scope).query(HistoryQuery(chat_ids=(scope['chat_id'],),
        start_recorded_at=time.time() - hours * 3600, limit=120, newest_first=True))
    return RecordBatch(tuple(RawRecord(
        id=m.ref.message_id, source=m.ref.source, stream_id=m.ref.chat_id,
        kind='wechat_message', actor=m.sender, time=m.time,
        text='\n'.join(part for part in (m.content,
            f'附件内容：{m.image_description}' if m.image_description and m.image_description != m.content
            else None) if part), content_hash=m.source_content_hash)
        for m in reversed(page.messages)), page.next_cursor is not None)


CONNECTORS = {'wechat': wechat_records, 'device': device_records}


def load_records(scope, hours):
    return CONNECTORS[scope.get('source_kind', 'wechat')](scope, hours)
