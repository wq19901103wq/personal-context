"""Source-neutral records passed from connectors to information processors."""

from dataclasses import dataclass
import hashlib
import json

from .history import MessageTime, SenderEvidence, SourceBinding


@dataclass(frozen=True)
class RawRecord:
    id: str
    source: SourceBinding
    stream_id: str
    kind: str
    actor: SenderEvidence
    time: MessageTime
    text: str
    content_hash: str


@dataclass(frozen=True)
class RecordBatch:
    records: tuple[RawRecord, ...]
    window_limited: bool = False
    new_record_ids: tuple[str, ...] | None = None


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def account_key(scope):
    return digest([scope['history_source'], scope['history_account']])


def source_key(scope):
    return digest([scope.get('source_kind', 'wechat'), account_key(scope), scope['chat_id']])
