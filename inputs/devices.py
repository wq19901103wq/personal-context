"""Authenticated final transcripts from speakers, voice satellites and other devices."""

from dataclasses import asdict
from contextlib import closing
from datetime import datetime
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from uuid import uuid4

from common.history import MessageTime, SenderEvidence, SourceBinding
from common.records import RawRecord, RecordBatch
from pathlib import Path
from common.records import digest, source_key


class DeviceConflict(ValueError):
    pass


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def text_field(data, key, *, default=None, limit=200):
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{key} 需要非空文字，最多 {limit} 字')
    return value.strip()


def stream_scope(base, device_id, name, stream_id):
    return dict(base, source_kind='device', device_id=device_id, device_stream_id=stream_id,
                chat_id=device_id + ':' + stream_id, chat_name=name, chat_type='device', extract_after=0)


def queue_in(db, scope):
    key = source_key(scope)
    db.execute('INSERT OR IGNORE INTO history_scans (source_key,scope_json,hours,created_at) '
               'VALUES (?,?,?,?)', (key, json.dumps(scope, ensure_ascii=False), 0, time.time()))
    return db.execute("SELECT id FROM history_scans WHERE source_key=? AND status IN ('queued','running')",
                      (key,)).fetchone()['id']


class DeviceInputs:
    def __init__(self, store):
        self.store = store

    def create(self, data):
        name = text_field(data, 'name')
        speaker = data.get('self_speaker_id') or None
        if speaker is not None:
            speaker = text_field(data, 'self_speaker_id')
        device_id, token = uuid4().hex, secrets.token_urlsafe(32)
        # Bind to the selected owner's context, just like a saved WeChat scan.
        scope = dict(self.store.settings(), assistant_knowledge=str(self.store.path))
        with self.store.connect() as db:
            db.execute('INSERT INTO device_sources '
                '(id,name,token_hash,scope_json,self_speaker_id,created_at) VALUES (?,?,?,?,?,?)',
                (device_id, name, token_hash(token), json.dumps(scope, ensure_ascii=False), speaker, time.time()))
        return dict(id=device_id, name=name, token=token, path='/ingest/devices/' + device_id)

    def list(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT d.id,d.name,d.enabled,d.self_speaker_id,d.created_at,'
                '(SELECT COUNT(*) FROM device_records r WHERE r.device_id=d.id) AS records,'
                '(SELECT COUNT(*) FROM device_records r WHERE r.device_id=d.id '
                'AND r.processed_at IS NULL) AS pending,'
                '(SELECT MAX(received_at) FROM device_records r WHERE r.device_id=d.id) AS last_received_at '
                'FROM device_sources d ORDER BY d.created_at DESC').fetchall()
        return [dict(r) for r in rows]

    def revoke(self, device_id):
        with self.store.connect() as db:
            if not db.execute('UPDATE device_sources SET enabled=0 WHERE id=?', (device_id,)).rowcount:
                raise KeyError(device_id)
        return dict(ok=True)

    def retry(self, device_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            device = db.execute('SELECT * FROM device_sources WHERE id=?', (device_id,)).fetchone()
            if device is None:
                raise KeyError(device_id)
            rows = db.execute('SELECT source_key,record_json FROM device_records '
                'WHERE device_id=? AND processed_at IS NULL GROUP BY source_key', (device_id,)).fetchall()
            base = json.loads(device['scope_json'])
            ids = [queue_in(db, stream_scope(base, device_id, device['name'],
                    json.loads(r['record_json'])['stream_id'].split(':', 1)[1])) for r in rows]
        return dict(scan_ids=ids)

    def receive(self, device_id, token, data):
        event_id = text_field(data, 'event_id')
        text = text_field(data, 'text', limit=20000)
        stream_id = text_field(data, 'stream_id', default='default')
        speaker_id = data.get('speaker_id') or None
        if speaker_id is not None:
            speaker_id = text_field(data, 'speaker_id')
        name = text_field(data, 'speaker_name', default='未识别说话者')
        sent = data.get('occurred_at')
        if isinstance(sent, str):
            parsed = datetime.fromisoformat(sent.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('occurred_at 请携带时区，例如 2026-11-05T18:00:00+08:00')
            sent = parsed.timestamp()
        if sent is not None and (isinstance(sent, bool) or not isinstance(sent, (int, float))
                                 or not math.isfinite(sent) or sent <= 0):
            raise ValueError('occurred_at 需要带时区的 ISO 时间或 Unix 秒数')
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            device = db.execute('SELECT * FROM device_sources WHERE id=? AND enabled=1', (device_id,)).fetchone()
            if not device or not hmac.compare_digest(device['token_hash'], token_hash(token)):
                raise PermissionError('设备凭据不可用')
            scope = stream_scope(json.loads(device['scope_json']), device_id, device['name'], stream_id)
            actor = SenderEvidence(source_sender_id=speaker_id or 'unknown:' + event_id, display_name=name,
                identity_basis='source_id' if speaker_id else 'unknown',
                is_self=True if speaker_id and speaker_id == device['self_speaker_id'] else None)
            content_hash = digest([text, stream_id, asdict(actor), sent])
            old = db.execute('SELECT record_json FROM device_records WHERE device_id=? AND event_id=?',
                             (device_id, event_id)).fetchone()
            if old:
                if json.loads(old['record_json'])['content_hash'] != content_hash:
                    raise DeviceConflict('event_id 已用于另一条内容；请仅提交最终转写，重试时保留原内容')
                return dict(received=False, duplicate=True, event_id=event_id)
            record = RawRecord(event_id, SourceBinding('device:' + device_id, scope['history_account']),
                scope['chat_id'], 'voice_transcript', actor, MessageTime(now, now, sent), text, content_hash)
            db.execute('INSERT INTO device_records '
                '(device_id,event_id,source_key,record_json,received_at) VALUES (?,?,?,?,?)',
                (device_id, event_id, source_key(scope), json.dumps(asdict(record), ensure_ascii=False), now))
            scan_id = queue_in(db, scope)
        return dict(received=True, duplicate=False, event_id=event_id, scan_id=scan_id)


def device_records(scope, hours):
    key = source_key(scope)
    with closing(sqlite3.connect(Path(scope['assistant_knowledge']).as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT seq,event_id,record_json FROM device_records WHERE source_key=? '
            'AND processed_at IS NULL ORDER BY seq LIMIT 81', (key,)).fetchall()
        pending = rows[:80]
        context = db.execute('SELECT seq,event_id,record_json FROM device_records WHERE source_key=? '
            'AND processed_at IS NOT NULL AND seq<? ORDER BY seq DESC LIMIT 30',
            (key, pending[0]['seq'] if pending else 2**63 - 1)).fetchall()
    records = []
    for row in [*reversed(context), *pending]:
        item = json.loads(row['record_json'])
        records.append(RawRecord(**dict(item, source=SourceBinding(**item['source']),
            actor=SenderEvidence(**item['actor']), time=MessageTime(**item['time']))))
    return RecordBatch(tuple(records), len(rows) > 80, tuple(r['event_id'] for r in pending))
