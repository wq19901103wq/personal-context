"""Connectors read original data; they do not infer tasks or decide notifications."""

import time

from common.history import HistoryQuery
from common.records import RecordBatch
from .wechat import history_reader, history_records
from .devices import device_records


def wechat_records(scope, hours):
    page = history_reader(scope).query(HistoryQuery(chat_ids=(scope['chat_id'],),
        start_recorded_at=time.time() - hours * 3600, limit=120, newest_first=True))
    return RecordBatch(history_records(scope, tuple(reversed(page.messages))), page.next_cursor is not None)


CONNECTORS = {'wechat': wechat_records, 'device': device_records}


def load_records(scope, hours):
    return CONNECTORS[scope.get('source_kind', 'wechat')](scope, hours)
