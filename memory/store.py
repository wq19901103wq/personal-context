"""SQLite owns work state; the original history and knowledge stay in place."""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import secrets
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from personal_context.memory import current_profile_sql, read_profiles, knowledge_signature
from personal_context.wechat_push import SESSION_ERRORS
from .personalization import next_recommendation_time
from common.recurrence import occurrences


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def recommendation_url(value):
    try:
        url = urlsplit(value.strip())
        if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
            return ''
        query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
        return urlunsplit((url.scheme, url.netloc.lower(), url.path, urlencode(query), ''))
    except ValueError:
        return ''


def recommendation_place_key(title, discovery):
    def normalized(value):
        return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())
    # Address keeps branches separate; links may differ between review platforms.
    value = [normalized(title), normalized(discovery.get('address', ''))]
    return hashlib.sha256(encode(value).encode()).hexdigest()


class TaskStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memory_questions (
                    id INTEGER PRIMARY KEY, question TEXT NOT NULL,
                    parent_id INTEGER REFERENCES memory_questions(id),
                    status TEXT NOT NULL DEFAULT 'queued', answer TEXT NOT NULL DEFAULT '',
                    error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY, description TEXT NOT NULL, notes TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open', due_at REAL, wake_at REAL,
                    timezone TEXT NOT NULL, followup_seconds REAL NOT NULL,
                    scope_json TEXT NOT NULL, observed_json TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                    trigger TEXT NOT NULL, context_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id),
                    title TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL, read_at REAL,
                    notification_status TEXT NOT NULL, notification_attempts INTEGER NOT NULL DEFAULT 0,
                    notification_error TEXT, retry_at REAL
                );
                CREATE INDEX IF NOT EXISTS reminders_task ON reminders(task_id, id);
                CREATE TABLE IF NOT EXISTS wechat_deliveries (
                    reminder_id INTEGER PRIMARY KEY REFERENCES reminders(id),
                    recipient_id TEXT NOT NULL, bot_id TEXT NOT NULL, client_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT, retry_at REAL, provider_message_id TEXT
                );
                CREATE TABLE IF NOT EXISTS task_links (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id), token TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS wechat_commands (
                    bot_id TEXT NOT NULL, recipient_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    task_id INTEGER REFERENCES tasks(id), command TEXT NOT NULL, reply TEXT NOT NULL,
                    client_id TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT, retry_at REAL NOT NULL,
                    PRIMARY KEY (bot_id, recipient_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS task_threads (
                    task_id INTEGER PRIMARY KEY REFERENCES tasks(id), thread_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                    instruction TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    plan_json TEXT NOT NULL DEFAULT '[]', result TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL DEFAULT '', error TEXT,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS executions_active ON executions(task_id)
                    WHERE status IN ('queued', 'running');
                CREATE TABLE IF NOT EXISTS execution_events (
                    id INTEGER PRIMARY KEY, execution_id INTEGER NOT NULL REFERENCES executions(id),
                    kind TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history_scans (
                    id INTEGER PRIMARY KEY, source_key TEXT NOT NULL, scope_json TEXT NOT NULL,
                    hours REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    result_json TEXT NOT NULL DEFAULT '{}', error TEXT,
                    created_at REAL NOT NULL, finished_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS history_scans_active ON history_scans(source_key)
                    WHERE status IN ('queued', 'running');
                CREATE TABLE IF NOT EXISTS history_scan_state (
                    source_key TEXT PRIMARY KEY, signature TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_records (
                    source_key TEXT NOT NULL, record_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                    scan_id INTEGER REFERENCES history_scans(id), processed_at REAL NOT NULL,
                    PRIMARY KEY (source_key, record_id)
                );
                CREATE TABLE IF NOT EXISTS device_sources (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL,
                    scope_json TEXT NOT NULL, self_speaker_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL REFERENCES device_sources(id),
                    event_id TEXT NOT NULL, source_key TEXT NOT NULL,
                    record_json TEXT NOT NULL, received_at REAL NOT NULL, processed_at REAL,
                    UNIQUE(device_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS device_records_pending
                    ON device_records(source_key, processed_at, seq);
                CREATE TABLE IF NOT EXISTS task_origins (
                    source_key TEXT NOT NULL, message_id TEXT NOT NULL,
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
                    PRIMARY KEY (source_key, message_id)
                );
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                    starts_at REAL, ends_at REAL, all_day INTEGER NOT NULL DEFAULT 0,
                    timezone TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
                    time_text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'confirmed',
                    remind_before_minutes INTEGER DEFAULT 30,
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS event_reminders (
                    id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES calendar_events(id),
                    starts_at REAL NOT NULL, remind_before_minutes INTEGER NOT NULL,
                    recipient_id TEXT NOT NULL, bot_id TEXT NOT NULL, client_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL, error TEXT, provider_message_id TEXT,
                    created_at REAL NOT NULL, sent_at REAL,
                    UNIQUE(event_id, starts_at)
                );
                CREATE TABLE IF NOT EXISTS item_evidence (
                    kind TEXT NOT NULL, item_id INTEGER NOT NULL, source_key TEXT NOT NULL,
                    record_id TEXT NOT NULL, content_hash TEXT NOT NULL, record_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (kind, item_id, source_key, record_id, content_hash)
                );
                CREATE TABLE IF NOT EXISTS extraction_items (
                    source_key TEXT NOT NULL, anchor_id TEXT NOT NULL, kind TEXT NOT NULL,
                    item_key TEXT NOT NULL, item_id INTEGER NOT NULL,
                    PRIMARY KEY (source_key, anchor_id, kind, item_key)
                );
                CREATE TABLE IF NOT EXISTS profile_facts (
                    id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, subject_key TEXT NOT NULL,
                    subject_name TEXT NOT NULL, identity_basis TEXT NOT NULL, is_self INTEGER,
                    category TEXT NOT NULL, value TEXT NOT NULL, confidence TEXT NOT NULL,
                    evidence_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    UNIQUE (subject_key, category, value)
                );
                CREATE TABLE IF NOT EXISTS insights (
                    id INTEGER PRIMARY KEY, source_key TEXT NOT NULL, anchor_id TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, reason TEXT NOT NULL,
                    profile_ids_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL, read_at REAL,
                    notification_status TEXT NOT NULL, notification_error TEXT,
                    client_id TEXT NOT NULL UNIQUE, recipient_id TEXT, bot_id TEXT, sent_at REAL,
                    UNIQUE (source_key, anchor_id)
                );
                CREATE TABLE IF NOT EXISTS proactive_runs (
                    run_key TEXT PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
                    context_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
                    summary TEXT, error TEXT, created_at REAL NOT NULL, finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS briefing_deliveries (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, items_json TEXT NOT NULL,
                    client_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
                    recipient_id TEXT, bot_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    error TEXT, created_at REAL NOT NULL, sent_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS briefing_daily ON briefing_deliveries(day)
                    WHERE kind='daily';
            """)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(insights)')}
            for name, declaration in {
                'kind': "TEXT NOT NULL DEFAULT 'chat_match'", 'url': "TEXT NOT NULL DEFAULT ''",
                'expires_at': 'REAL', 'context_json': "TEXT NOT NULL DEFAULT '{}'",
                'notification_attempts': 'INTEGER NOT NULL DEFAULT 0',
                'retry_at': 'REAL NOT NULL DEFAULT 0',
                'discovery_json': "TEXT NOT NULL DEFAULT '{}'",
                'not_before': 'REAL NOT NULL DEFAULT 0',
                'occasion_json': "TEXT NOT NULL DEFAULT '{}'",
                'place_key': "TEXT NOT NULL DEFAULT ''",
                'feedback': "TEXT NOT NULL DEFAULT ''", 'feedback_at': 'REAL',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE insights ADD COLUMN {name} {declaration}')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(proactive_runs)')}
            if 'research_json' not in columns:
                db.execute("ALTER TABLE proactive_runs ADD COLUMN research_json TEXT NOT NULL DEFAULT '{}'")
            if 'requested' not in columns:
                db.execute('ALTER TABLE proactive_runs ADD COLUMN requested INTEGER NOT NULL DEFAULT 0')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(calendar_events)')}
            if 'remind_before_minutes' not in columns:
                db.execute('ALTER TABLE calendar_events ADD COLUMN remind_before_minutes INTEGER DEFAULT 30')
            if 'scheduled_date' not in columns:
                db.execute('ALTER TABLE calendar_events ADD COLUMN scheduled_date TEXT')
            if 'time_period' not in columns:
                db.execute("ALTER TABLE calendar_events ADD COLUMN time_period TEXT NOT NULL DEFAULT ''")
            if 'recurrence_json' not in columns:
                db.execute('ALTER TABLE calendar_events ADD COLUMN recurrence_json TEXT')
            for table in ('tasks', 'calendar_events'):
                columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                if 'deleted_at' not in columns:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN deleted_at REAL')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(profile_facts)')}
            for name, declaration in {
                'observed_at': 'REAL', 'updated_at': 'REAL', 'valid_from': 'REAL', 'valid_until': 'REAL',
                'temporal_kind': "TEXT NOT NULL DEFAULT 'durable'", 'superseded_by': 'INTEGER',
                'field': "TEXT NOT NULL DEFAULT ''",
                'revision': 'INTEGER NOT NULL DEFAULT 0',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE profile_facts ADD COLUMN {name} {declaration}')
            db.execute('UPDATE profile_facts SET observed_at=COALESCE('
                "json_extract(evidence_json,'$[0].time.sent_at'),"
                "json_extract(evidence_json,'$[0].time.first_observed_at'),created_at),"
                'updated_at=created_at WHERE updated_at IS NULL')
            # Carry forward the last successful window when upgrading. Unlike the
            # window signature, this ledger survives messages leaving that window.
            db.execute('INSERT OR IGNORE INTO processed_records '
                '(source_key,record_id,content_hash,processed_at) '
                'SELECT s.source_key,j.key,j.value,? FROM history_scan_state s,json_each('
                "CASE WHEN json_valid(s.signature) THEN s.signature ELSE '{}' END) j "
                "WHERE j.type='text'", (time.time(),))
        self.remove_extraction_annotations()

    def backup(self):
        directory = self.path.parent / 'backups'
        directory.mkdir(exist_ok=True)
        path = directory / f'{self.path.stem}-{time.time_ns()}.db'
        with self.connect() as source, sqlite3.connect(path) as target:
            source.backup(target)
        return path

    def remove_extraction_annotations(self):
        """Remove obsolete correction prose without changing the user's item state."""
        with self.connect() as db:
            affected = [(table, dict(row)) for table in ('tasks', 'calendar_events')
                        for row in db.execute(f'SELECT id,notes,scope_json FROM {table} '
                            "WHERE json_type(scope_json,'$.user_correction') IS NOT NULL")]
        if not affected:
            return
        self.backup()
        with self.connect() as db:
            for table, row in affected:
                scope = json.loads(row['scope_json'])
                correction = scope.pop('user_correction')
                notes = '' if row['notes'] == correction else row['notes']
                db.execute(f'UPDATE {table} SET notes=?,scope_json=? WHERE id=? AND scope_json=? AND notes=?',
                           (notes, encode(scope), row['id'], row['scope_json'], row['notes']))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def task_row(row):
        item = dict(row)
        item['scope'] = json.loads(item.pop('scope_json'))
        raw = item.pop('observed_json')
        item['observed'] = None if raw is None else json.loads(raw)
        return item

    @contextmanager
    def worker_lock(self):
        # The UI worker and a manual CLI tick may use the same database.
        with self.path.with_suffix('.worker.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    @contextmanager
    def delivery_lock(self):
        # Source reads may be slow. Only competing senders share this lock.
        with self.path.with_suffix('.delivery.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True

    def settings(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='sources'").fetchone()
        return json.loads(row['value']) if row else default_sources()

    def set_settings(self, sources):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES ('sources', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encode(sources),))

    def personalization(self):
        with self.connect() as db:
            return self.personalization_in(db)

    @staticmethod
    def personalization_in(db):
        row = db.execute("SELECT value FROM settings WHERE key='personalization'").fetchone()
        return json.loads(row['value']) if row else dict(enabled=False, categories=[])

    def set_personalization(self, value):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES ('personalization',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encode(value),))

    def recommendation_context(self, now=None):
        with self.connect() as db:
            return self.recommendation_context_in(db, now)

    def recommendation_context_in(self, db, now=None):
        # Recheck this snapshot before saving/sending after a slow search.
        now = time.time() if now is None else now
        row = db.execute("SELECT value FROM settings WHERE key='sources'").fetchone()
        sources = json.loads(row[0]) if row else default_sources()
        return dict(recommendation_policy=2, preferences=self.personalization_in(db),
            events=[list(r) for r in db.execute("SELECT id,revision FROM calendar_events "
                "WHERE deleted_at IS NULL AND status!='cancelled' ORDER BY id")],
            profiles=[list(r) for r in db.execute('SELECT id,revision FROM profile_facts WHERE ' +
                current_profile_sql() + " AND is_self=1 AND confidence='explicit' ORDER BY id",
                (now, now))],
            sources=[list(r) for r in db.execute("SELECT value FROM settings WHERE key='sources'")],
            knowledge=knowledge_signature(sources.get('dh_knowledge', [])))

    def start_proactive(self, key, trigger, versions, *, day, now, requested=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if (versions != self.recommendation_context_in(db, now)
                    or (not requested and db.execute('SELECT COUNT(*) FROM proactive_runs '
                        'WHERE day=? AND requested=0', (day,)).fetchone()[0] >= 2)):
                return False
            return bool(db.execute('INSERT OR IGNORE INTO proactive_runs '
                '(run_key,kind,day,context_json,created_at,requested) VALUES (?,?,?,?,?,?)',
                (key, trigger['kind'], day, encode(versions), now, int(requested))).rowcount)

    def finish_proactive(self, key, trigger, versions, context, output, *, wechat=None, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM proactive_runs WHERE run_key=?', (key,)).fetchone()
            if not row or row['status'] != 'running':
                return []
            if versions != self.recommendation_context_in(db, now) or trigger['expires_at'] <= now:
                db.execute("UPDATE proactive_runs SET status='superseded',finished_at=? WHERE run_key=?", (now, key))
                return []
            profiles = {p['id']: p for p in context['chat_profiles']}
            facts = {f['id']: f for f in context['dh_facts']}
            recent = db.execute('SELECT title,url FROM insights WHERE created_at>?', (now - 7*86400,)).fetchall()
            titles, urls = {r['title'].strip().casefold() for r in recent}, {r['url'] for r in recent}
            rejected = self.rejected_recommendations_in(db)
            rejected_keys = {i['place_key'] for i in rejected}
            events = [self.event_row(r) for r in db.execute('SELECT * FROM calendar_events WHERE deleted_at IS NULL')]
            sources = db.execute("SELECT value FROM settings WHERE key='sources'").fetchone()
            timezone = (json.loads(sources[0]) if sources else default_sources())['timezone']
            ids = []
            for item in output['items'][:2]:
                url = recommendation_url(item['url'])
                title = item['title'].strip()
                place_key = recommendation_place_key(title, item.get('discovery', {}))
                if place_key in rejected_keys:
                    continue
                if not url or not title or url in urls or title.casefold() in titles:
                    continue
                if any(pid not in profiles for pid in item['profile_ids']) or any(
                        fid not in facts for fid in item['dh_fact_ids']):
                    continue
                evidence = [dict(kind='dh_fact', **facts[fid]) for fid in item['dh_fact_ids']]
                for pid in item['profile_ids']:
                    saved = db.execute('SELECT evidence_json FROM profile_facts WHERE id=?', (pid,)).fetchone()
                    evidence.extend(json.loads(saved[0]))
                used_memories = [m for m in context.get('memories', []) if
                    m.get('profile_id') in item['profile_ids'] or m.get('dh_fact_id') in item['dh_fact_ids']]
                expires_at = min([trigger['expires_at']] + [m['valid_until'] for m in used_memories
                    if m.get('valid_until') is not None])
                if expires_at <= now:
                    continue
                not_before = next_recommendation_time(events, now, expires_at, timezone)
                cursor = db.execute('INSERT INTO insights '
                    '(source_key,anchor_id,title,body,reason,profile_ids_json,evidence_json,created_at,'
                    'notification_status,client_id,recipient_id,bot_id,kind,url,expires_at,context_json,discovery_json,'
                    'not_before,occasion_json,place_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('proactive', f'{key}:{len(ids)}', title, item['body'], item['reason'],
                     encode(item['profile_ids']), encode(evidence), now,
                     ('pending' if wechat else 'disabled') if not_before is not None else 'expired',
                     'insight-' + uuid4().hex, wechat.recipient_id if wechat else None,
                     wechat.bot_id if wechat else None, trigger['kind'], url, expires_at,
                     encode(versions), encode(item.get('discovery', {})), not_before or 0,
                     encode(trigger), place_key))
                ids.append(cursor.lastrowid)
                titles.add(title.casefold())
                urls.add(url)
            db.execute("UPDATE proactive_runs SET status='completed',summary=?,research_json=?,finished_at=? WHERE run_key=?",
                       (output['summary'], encode(output.get('research', {})), now, key))
            return ids

    def fail_proactive(self, key, error):
        with self.connect() as db:
            db.execute("UPDATE proactive_runs SET status='failed',error=?,finished_at=? "
                       "WHERE run_key=? AND status='running'", (error, time.time(), key))

    def recover_proactive(self):
        with self.connect() as db:
            db.execute("UPDATE proactive_runs SET status='interrupted',finished_at=? WHERE status='running'",
                       (time.time(),))

    def expire_insights_in(self, db, now):
        db.execute("UPDATE insights SET notification_status='expired' "
            "WHERE notification_status IN ('pending','retry','disabled') AND "
            "(expires_at<=? OR (expires_at IS NULL AND created_at<?))", (now, now - 86400))
        current = self.recommendation_context_in(db, now)
        for row in db.execute("SELECT id,context_json FROM insights WHERE kind!='chat_match' "
                              "AND notification_status IN ('pending','retry','disabled')").fetchall():
            if json.loads(row['context_json']) != current:
                db.execute("UPDATE insights SET notification_status='superseded' WHERE id=?", (row['id'],))

    def create(self, *, description, notes='', due_at=None, wake_at=None,
               timezone='Asia/Shanghai', followup_seconds=86400, scope=None, observed=None,
               now=None):
        now = time.time() if now is None else now
        wake_at = wake_at if wake_at is not None else (due_at if due_at is not None else now)
        with self.connect() as db:
            task_id = self.create_in(db, description=description, notes=notes, due_at=due_at,
                wake_at=wake_at, timezone=timezone, followup_seconds=followup_seconds,
                scope=scope, observed=observed, now=now)
        return self.get(task_id)

    def create_in(self, db, *, description, notes='', due_at=None, wake_at=None,
                  timezone='Asia/Shanghai', followup_seconds=86400, scope=None, observed=None, status='open', now):
        wake_at = wake_at if wake_at is not None else (due_at if due_at is not None else now)
        cursor = db.execute("""
                INSERT INTO tasks (description, notes, due_at, wake_at, timezone, followup_seconds, status,
                                   scope_json, observed_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (description, notes, due_at, wake_at, timezone, followup_seconds,
                  status, encode(scope or {}), None if observed is None else encode(observed), now, now))
        return cursor.lastrowid

    def queue_scan(self, source_key, scope, hours):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO history_scans (source_key,scope_json,hours,created_at) "
                       "VALUES (?,?,?,?)", (source_key, encode(scope), hours, time.time()))
            row = db.execute("SELECT id FROM history_scans WHERE source_key=? AND status IN "
                             "('queued','running')", (source_key,)).fetchone()
        return row['id']

    def scans(self):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM history_scans ORDER BY id DESC LIMIT 30').fetchall()
        return [dict(id=r['id'], chat_name=json.loads(r['scope_json']).get('chat_name'),
                     source_kind=json.loads(r['scope_json']).get('source_kind', 'wechat'),
                     status=r['status'], result=json.loads(r['result_json']), error=r['error'],
                     created_at=r['created_at']) for r in rows]

    def scan_watch(self, config=None):
        with self.connect() as db:
            if config is not None:
                db.execute("INSERT INTO settings VALUES ('extraction_watch',?) ON CONFLICT(key) "
                           "DO UPDATE SET value=excluded.value", (encode(config),))
            row = db.execute("SELECT value FROM settings WHERE key='extraction_watch'").fetchone()
        return json.loads(row[0]) if row else dict(enabled=False)

    def advance_scan_watch(self, expected, config, scans):
        """Commit the activity checkpoint with its queued work, unless stopped or replaced."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM settings WHERE key='extraction_watch'").fetchone()
            if row is None or json.loads(row[0]) != expected:
                return
            for key, scope, activity in scans:
                active = db.execute("SELECT status FROM history_scans WHERE source_key=? "
                                    "AND status IN ('queued','running')", (key,)).fetchone()
                # A running scan may already have read its input; leave this activity for the next check.
                if active and active['status'] == 'running':
                    continue
                if not active:
                    db.execute('INSERT INTO history_scans (source_key,scope_json,hours,created_at) '
                               'VALUES (?,?,?,?)', (key, encode(scope), config['hours'], time.time()))
                if config.get('mode') == 'all':
                    config['seen'][scope['chat_id']] = activity
            db.execute("UPDATE settings SET value=? WHERE key='extraction_watch'", (encode(config),))

    def claim_scan(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM history_scans WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE history_scans SET status='running' WHERE id=?", (row['id'],))
        return dict(row) if row else None

    def recover_scans(self):
        with self.connect() as db:
            # Extraction has no external actions and saves each result atomically, so it can resume safely.
            db.execute("UPDATE history_scans SET status='queued',error=NULL WHERE status='running'")

    def scan_signature(self, source_key):
        with self.connect() as db:
            row = db.execute('SELECT signature FROM history_scan_state WHERE source_key=?',
                             (source_key,)).fetchone()
        return row[0] if row else None

    def unprocessed_records(self, source_key, records):
        if not records:
            return []
        with self.connect() as db:
            versions = {r['record_id']: r['content_hash'] for r in db.execute(
                'SELECT record_id,content_hash FROM processed_records WHERE source_key=? AND record_id IN (' +
                ','.join('?' for _ in records) + ')', (source_key, *(r.id for r in records)))}
        return [r.id for r in records if versions.get(r.id) != r.content_hash]

    def save_profile_in(self, db, scan, fact, *, now):
        old = None
        if fact.get('profile_id') is not None:
            old = db.execute('SELECT * FROM profile_facts WHERE id=?', (fact['profile_id'],)).fetchone()
            if (old is None or old['status'] not in ('active', 'superseded')
                    or old['revision'] != fact['expected_revision']
                    or (old['account_key'], old['subject_key'], old['category']) !=
                       (fact['account_key'], fact['subject_key'], fact['category'])
                    or fact['confidence'] != 'explicit'
                    or fact['observed_at'] < old['observed_at']):
                return None
            if fact['operation'] == 'remember' and fact['value'] != old['value']:
                return None
        if fact.get('operation') == 'retract':
            if old is None:
                return None
            boundary = fact['valid_from'] if fact['valid_from'] is not None else fact['observed_at']
            db.execute('UPDATE profile_facts SET status=?,'
                       'valid_until=CASE WHEN valid_until IS NULL THEN ? ELSE MIN(valid_until,?) END,'
                       'observed_at=?,updated_at=?,revision=revision+1 WHERE id=?',
                       ('superseded' if boundary > now else 'retracted', boundary, boundary,
                        fact['observed_at'], now, old['id']))
            profile_id = old['id']
        else:
            existing = db.execute('SELECT * FROM profile_facts WHERE subject_key=? AND category=? AND value=?',
                (fact['subject_key'], fact['category'], fact['value'])).fetchone()
            if existing is not None:
                # Replaying an older statement never revives a removed fact.
                if (existing['status'] == 'dismissed' or fact['observed_at'] < existing['observed_at']
                        or (existing['status'] != 'active' and
                            (fact['confidence'] != 'explicit' or fact['observed_at'] <= existing['observed_at']))):
                    return None
                profile_id = existing['id']
                if not (existing['confidence'] == 'explicit' and fact['confidence'] != 'explicit'):
                    db.execute("UPDATE profile_facts SET status='active',superseded_by=NULL,"
                        'observed_at=?,updated_at=?,evidence_json=?,confidence=?,'
                        'valid_from=?,valid_until=?,temporal_kind=?,field=?,revision=revision+1 WHERE id=?',
                        (fact['observed_at'], now, encode(fact['evidence']), fact['confidence'],
                         fact['valid_from'], fact['valid_until'], fact['temporal_kind'], fact['field'], profile_id))
            else:
                cursor = db.execute('INSERT INTO profile_facts '
                    '(account_key,subject_key,subject_name,identity_basis,is_self,category,value,confidence,'
                    'evidence_json,created_at,observed_at,updated_at,valid_from,valid_until,temporal_kind,field) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (fact['account_key'], fact['subject_key'], fact['subject_name'], fact['identity_basis'],
                     fact['is_self'], fact['category'], fact['value'], fact['confidence'], encode(fact['evidence']),
                     now, fact['observed_at'], now, fact['valid_from'], fact['valid_until'], fact['temporal_kind'], fact['field']))
                profile_id = cursor.lastrowid
            if old is not None and old['id'] != profile_id:
                boundary = fact['valid_from'] if fact['valid_from'] is not None else fact['observed_at']
                db.execute("UPDATE profile_facts SET status='superseded',superseded_by=?,"
                    'valid_until=CASE WHEN valid_until IS NULL THEN ? ELSE MIN(valid_until,?) END,'
                    'updated_at=?,revision=revision+1 WHERE id=?', (profile_id, boundary, boundary, now, old['id']))
        for record in fact['evidence']:
            db.execute('INSERT OR IGNORE INTO item_evidence VALUES (?,?,?,?,?,?,?)',
                ('profile', profile_id, scan['source_key'], record['id'], record['content_hash'], encode(record), now))
        return profile_id

    def apply_extracted_in(self, db, scan, kind, item, *, now):
        """Apply a sourced change once; a concurrent user edit takes precedence."""
        table, title = ('tasks', 'description') if kind == 'task' else ('calendar_events', 'title')
        item_id = item.get(kind + '_id')
        created, changed = False, False
        if item_id is not None:
            row = db.execute(f'SELECT * FROM {table} WHERE id=?', (item_id,)).fetchone()
            if row is None or row['deleted_at'] is not None or row['updated_at'] != item['expected_updated_at']:
                return None, 'skipped'
            if item.get('discard'):
                scope = json.loads(row['scope_json'])
                if not (scope.get('origin') or scope.get('involvement')):
                    return None, 'skipped'
                self.set_deleted_in(db, kind, item_id, True, now=now)
                db.execute(f"UPDATE {table} SET notes='' WHERE id=?", (item_id,))
                return item_id, 'discarded'
            fields = item['fields']
            if row['status'] in ('done', 'cancelled') and fields.get('status') not in (None, row['status']):
                return None, 'skipped'
            changes = {key: value for key, value in fields.items()
                       if key in row.keys() and key != 'timezone' and row[key] != value}
            if changes:
                if kind == 'task':
                    self.update_in(db, item_id, changes, now=now)
                else:
                    self.update_event_in(db, item_id, changes, now=now)
                changed = True
        else:
            fields = item['fields']
            key = fields[title].strip().casefold()
            origin = db.execute('SELECT item_id FROM extraction_items WHERE source_key=? '
                'AND anchor_id=? AND kind=? AND item_key=?',
                (scan['source_key'], item['anchor_message_id'], kind, key)).fetchone()
            # Preserve the old single-anchor links while allowing several different items per message.
            if origin is None and kind == 'task':
                origin = db.execute('SELECT t.id AS item_id FROM task_origins o JOIN tasks t '
                    'ON t.id=o.task_id WHERE o.source_key=? AND o.message_id=? AND t.description=?',
                    (scan['source_key'], item['anchor_message_id'], fields[title])).fetchone()
            if origin:
                item_id = origin['item_id']
            else:
                create = self.create_in if kind == 'task' else self.create_event_in
                item_id = create(db, **fields, now=now)
                created = True
        for record in item.get('evidence', []):
            db.execute('INSERT OR IGNORE INTO item_evidence VALUES (?,?,?,?,?,?,?)',
                (kind, item_id, scan['source_key'], record['id'], record['content_hash'], encode(record), now))
        db.execute('INSERT OR IGNORE INTO extraction_items VALUES (?,?,?,?,?)',
            (scan['source_key'], item['anchor_message_id'], kind,
             fields[title].strip().casefold(), item_id))
        return item_id, 'created' if created else 'updated' if changed else 'unchanged'

    def finish_scan(self, scan, *, candidates=(), events=(), profiles=(), insights=(), wechat=None,
                    signature=None, result=None, error=None, consumed_record_ids=(), processed_records=()):
        now, profile_ids, insight_ids = time.time(), [], []
        applied = dict(created_task_ids=[], updated_task_ids=[], discarded_task_ids=[],
                       created_event_ids=[], updated_event_ids=[], discarded_event_ids=[])
        skipped = 0
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT status,result_json FROM history_scans WHERE id=?', (scan['id'],)).fetchone()
            if saved and saved['status'] == 'completed':
                return json.loads(saved['result_json'])
            for kind, items in (('task', candidates), ('event', events)):
                for item in items:
                    item_id, action = self.apply_extracted_in(db, scan, kind, item, now=now)
                    if action == 'skipped':
                        skipped += 1
                    elif action in ('created', 'updated', 'discarded'):
                        applied[f'{action}_{kind}_ids'].append(item_id)
            for fact in profiles:
                profile_ids.append(self.save_profile_in(db, scan, fact, now=now))
            for item in insights:
                linked = list(dict.fromkeys([*item['existing_profile_ids'],
                    *(profile_ids[i] for i in item['new_profile_indexes'])]))
                # Dismissed or unconfirmed identities never become a personal recommendation.
                if not linked or any(not db.execute('SELECT 1 FROM profile_facts WHERE id=? AND ' +
                    current_profile_sql() + " AND is_self=1 AND confidence='explicit'", (pid, now, now)).fetchone()
                    for pid in linked):
                    continue
                cursor = db.execute('INSERT OR IGNORE INTO insights '
                    '(source_key,anchor_id,title,body,reason,profile_ids_json,evidence_json,created_at,'
                    'notification_status,client_id,recipient_id,bot_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (scan['source_key'], item['anchor_record_id'], item['title'], item['body'], item['reason'],
                     encode(linked), encode(item['evidence']), now, 'pending' if wechat else 'disabled',
                     'insight-' + uuid4().hex, wechat.recipient_id if wechat else None,
                     wechat.bot_id if wechat else None))
                if cursor.rowcount:
                    insight_ids.append(cursor.lastrowid)
            if signature is not None:
                db.execute('INSERT INTO history_scan_state VALUES (?,?) ON CONFLICT(source_key) '
                           'DO UPDATE SET signature=excluded.signature', (scan['source_key'], signature))
            if not error:
                db.executemany('INSERT INTO processed_records VALUES (?,?,?,?,?) '
                    'ON CONFLICT(source_key,record_id) DO UPDATE SET content_hash=excluded.content_hash,'
                    'scan_id=excluded.scan_id,processed_at=excluded.processed_at',
                    [(scan['source_key'], r.id, r.content_hash, scan['id'], now) for r in processed_records])
            result = dict(result or {}, **applied, skipped_updates=skipped,
                          profile_ids=[pid for pid in profile_ids if pid is not None], created_insight_ids=insight_ids)
            db.execute('UPDATE history_scans SET status=?,result_json=?,error=?,finished_at=? WHERE id=?',
                       ('failed' if error else 'completed', encode(result), error, now, scan['id']))
            if not error and json.loads(scan['scope_json']).get('source_kind') == 'device':
                db.executemany('UPDATE device_records SET processed_at=? WHERE source_key=? AND event_id=?',
                               [(now, scan['source_key'], rid) for rid in consumed_record_ids])
                # A new event can arrive after the running scan read its batch.
                if db.execute('SELECT 1 FROM device_records WHERE source_key=? AND processed_at IS NULL',
                              (scan['source_key'],)).fetchone():
                    db.execute('INSERT INTO history_scans (source_key,scope_json,hours,created_at) '
                               'VALUES (?,?,?,?)',
                               (scan['source_key'], scan['scope_json'], scan['hours'], now))
        return result

    def profiles(self, *, account_key=None, include_dismissed=False, now=None):
        with self.connect() as db:
            return read_profiles(db, account_key=account_key, include_inactive=include_dismissed, now=now)

    def dismiss_profile(self, profile_id):
        with self.connect() as db:
            db.execute("UPDATE profile_facts SET status='dismissed',updated_at=?,revision=revision+1 WHERE id=?",
                       (time.time(), profile_id))
            rows = db.execute("SELECT id,profile_ids_json FROM insights "
                              "WHERE notification_status IN ('pending','retry')").fetchall()
            for row in rows:
                if profile_id in json.loads(row['profile_ids_json']):
                    db.execute("UPDATE insights SET notification_status='superseded' WHERE id=?", (row['id'],))

    def recent_recommendations(self, now):
        with self.connect() as db:
            rows = db.execute('SELECT title,url,created_at FROM insights ORDER BY id DESC LIMIT 50').fetchall()
        return [dict(title=r['title'], url=r['url']) for r in rows if r['created_at'] > now - 7 * 86400]

    def insights(self):
        with self.connect() as db:
            self.expire_insights_in(db, time.time())
            rows = db.execute('SELECT * FROM insights ORDER BY id DESC LIMIT 50').fetchall()
        items = [dict(r) for r in rows]
        for item in items:
            item['evidence'] = json.loads(item.pop('evidence_json'))
            item['profile_ids'] = json.loads(item.pop('profile_ids_json'))
            item['discovery'] = json.loads(item.pop('discovery_json'))
            item.pop('context_json')
            item.pop('occasion_json')
        return items

    def read_insight(self, insight_id):
        with self.connect() as db:
            db.execute("UPDATE insights SET read_at=?,notification_status=CASE "
                       "WHEN notification_status IN ('pending','retry') THEN 'superseded' ELSE notification_status END "
                       "WHERE id=?", (time.time(), insight_id))

    def rejected_recommendations_in(self, db):
        result = []
        for row in db.execute("SELECT title,url,place_key,discovery_json FROM insights "
                              "WHERE feedback='not_interested' ORDER BY feedback_at DESC,id DESC"):
            discovery = json.loads(row['discovery_json'])
            result.append(dict(title=row['title'], url=row['url'], address=discovery.get('address', ''),
                place_key=row['place_key'] or recommendation_place_key(row['title'], discovery)))
        return result

    def rejected_recommendations(self):
        with self.connect() as db:
            return self.rejected_recommendations_in(db)

    def dismiss_insight(self, insight_id):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM insights WHERE id=?', (insight_id,)).fetchone()
            if row is None:
                raise KeyError('推荐不存在')
            key = row['place_key'] or recommendation_place_key(row['title'], json.loads(row['discovery_json']))
            db.execute("UPDATE insights SET feedback='not_interested',feedback_at=?,place_key=?,read_at=? "
                       "WHERE id=?", (now, key, now, insight_id))
            for pending in db.execute("SELECT * FROM insights WHERE notification_status IN "
                                      "('pending','retry','disabled')").fetchall():
                other = pending['place_key'] or recommendation_place_key(pending['title'], json.loads(pending['discovery_json']))
                if other == key:
                    db.execute("UPDATE insights SET notification_status='dismissed' WHERE id=?", (pending['id'],))

    def claim_insight(self, now, *, respect_schedule=True):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.expire_insights_in(db, now)
            sent = db.execute('SELECT COUNT(*),MAX(sent_at) FROM insights WHERE sent_at>?',
                              (now - 86400,)).fetchone()
            if sent[0] >= 2 or (sent[1] is not None and now - sent[1] < 4 * 3600):
                return None
            rows = db.execute("SELECT * FROM insights WHERE notification_status IN ('pending','retry') "
                              "AND retry_at<=? ORDER BY id", (now,)).fetchall()
            row = None
            events = [self.event_row(r) for r in db.execute('SELECT * FROM calendar_events WHERE deleted_at IS NULL')]
            sources = db.execute("SELECT value FROM settings WHERE key='sources'").fetchone()
            timezone = (json.loads(sources[0]) if sources else default_sources())['timezone']
            for candidate in rows:
                if respect_schedule:
                    if candidate['not_before'] > now:
                        continue
                    due = next_recommendation_time(events, now, candidate['expires_at'] or
                                                  candidate['created_at'] + 86400, timezone)
                    if due is None:
                        db.execute("UPDATE insights SET notification_status='expired' WHERE id=?", (candidate['id'],))
                        continue
                    db.execute('UPDATE insights SET not_before=? WHERE id=?', (due, candidate['id']))
                    if due > now:
                        continue
                row = candidate
                break
            if row:
                db.execute("UPDATE insights SET notification_status='sending',sent_at=?, "
                           "notification_attempts=notification_attempts+1 WHERE id=?",
                           (now, row['id']))
        return dict(row) if row else None

    def insight_result(self, insight_id, *, status, error=None, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("UPDATE insights SET notification_status=CASE "
                       "WHEN ?='retry' AND notification_attempts>=3 THEN 'failed' ELSE ? END, "
                       "notification_error=?,retry_at=?, "
                       "sent_at=CASE WHEN ? IN ('retry','failed') THEN NULL ELSE sent_at END "
                       "WHERE id=? AND notification_status='sending'",
                       (status, status, error, now + 60, status, insight_id))

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f'待办 #{task_id} 不存在')
        return self.task_row(row)

    def tasks(self, *, active_only=False, include_deleted=False):
        conditions = [] if include_deleted else ['deleted_at IS NULL']
        if active_only:
            conditions.append("status IN ('open', 'waiting')")
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks" + where +
                              " ORDER BY status IN ('done', 'cancelled'), "
                              "COALESCE(wake_at, 1e30), id DESC").fetchall()
        return [self.task_row(row) for row in rows]

    def evidence(self, kind, item_id):
        with self.connect() as db:
            rows = db.execute('SELECT record_json FROM item_evidence WHERE kind=? AND item_id=? '
                              'ORDER BY created_at, record_id', (kind, item_id)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def evidence_for(self, kind, item_ids):
        item_ids = tuple(item_ids)
        if not item_ids:
            return {}
        with self.connect() as db:
            rows = db.execute('SELECT item_id,record_json FROM item_evidence WHERE kind=? AND item_id IN (' +
                              ','.join('?' for _ in item_ids) + ') ORDER BY created_at,record_id',
                              (kind, *item_ids)).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row['item_id'], []).append(json.loads(row['record_json']))
        return grouped

    def create_event_in(self, db, *, title, notes='', starts_at=None, ends_at=None, all_day=False,
                        timezone='Asia/Shanghai', location='', time_text='', status='confirmed',
                        scheduled_date=None, time_period='', remind_before_minutes=30, scope=None, recurrence=None, now):
        return db.execute('INSERT INTO calendar_events '
            '(title,notes,starts_at,ends_at,all_day,timezone,location,time_text,status,scope_json,created_at,updated_at,remind_before_minutes,scheduled_date,time_period,recurrence_json) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (title, notes, starts_at, ends_at, int(all_day), timezone, location, time_text,
             status, encode(scope or {}), now, now, remind_before_minutes, scheduled_date, time_period, encode(recurrence) if recurrence else None)).lastrowid

    def create_event(self, **fields):
        with self.connect() as db:
            event_id = self.create_event_in(db, **fields, now=time.time())
        return self.event(event_id)

    @staticmethod
    def event_row(row):
        event = dict(row)
        event['scope'] = json.loads(event.pop('scope_json'))
        event['all_day'] = bool(event['all_day'])
        event['recurrence'] = json.loads(event.pop('recurrence_json') or 'null')
        return event

    def event(self, event_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM calendar_events WHERE id=?', (event_id,)).fetchone()
        if row is None:
            raise KeyError(f'日程 #{event_id} 不存在')
        event = self.event_row(row)
        event['evidence'] = self.evidence('event', event_id)
        with self.connect() as db:
            reminder = db.execute('SELECT status,error,sent_at FROM event_reminders '
                'WHERE event_id=? AND starts_at=?', (event_id, event['starts_at'])).fetchone()
        event['reminder'] = dict(reminder) if reminder else None
        return event

    def events(self, *, include_deleted=False):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM calendar_events ' +
                              ('' if include_deleted else 'WHERE deleted_at IS NULL ') +
                              'ORDER BY status=\'cancelled\', starts_at IS NULL, starts_at, id').fetchall()
            evidence = db.execute("SELECT item_id,record_json FROM item_evidence WHERE kind='event' "
                                  'ORDER BY created_at,record_id').fetchall()
            reminders = {(r['event_id'], r['starts_at']): dict(r) for r in db.execute(
                'SELECT event_id,starts_at,status,error,sent_at FROM event_reminders')}
        grouped = {}
        for record in evidence:
            grouped.setdefault(record['item_id'], []).append(json.loads(record['record_json']))
        return [dict(self.event_row(row), evidence=grouped.get(row['id'], []),
                     reminder=reminders.get((row['id'], row['starts_at']))) for row in rows]

    def update_event_in(self, db, event_id, changes, *, now):
        row = db.execute('SELECT deleted_at FROM calendar_events WHERE id=?', (event_id,)).fetchone()
        if row is not None and row['deleted_at'] is not None:
            raise ValueError('日程已删除')
        changes = dict(changes)
        if 'recurrence' in changes:
            changes['recurrence_json'] = encode(changes.pop('recurrence'))
        changes = {key: value for key, value in changes.items() if key in {
            'title', 'notes', 'starts_at', 'ends_at', 'all_day', 'timezone', 'location', 'time_text', 'status',
            'scope_json', 'remind_before_minutes', 'scheduled_date', 'time_period', 'recurrence_json'}}
        changes['updated_at'] = now
        sql = ', '.join(f'{column}=?' for column in changes)
        cursor = db.execute(f'UPDATE calendar_events SET {sql},revision=revision+1 WHERE id=?',
                            (*changes.values(), event_id))
        if not cursor.rowcount:
            raise KeyError(f'日程 #{event_id} 不存在')
        db.execute("UPDATE event_reminders SET status='superseded' WHERE event_id=? "
                   "AND status IN ('pending','retry')", (event_id,))

    def update_event(self, event_id, changes):
        with self.connect() as db:
            self.update_event_in(db, event_id, changes, now=time.time())
        return self.event(event_id)

    def update(self, task_id, changes, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.update_in(db, task_id, changes, now=now)
        return self.get(task_id)

    def update_in(self, db, task_id, changes, *, now):
        # Column names come from this fixed list, never from SQL supplied by a caller.
        changes = {key: value for key, value in changes.items() if key in {
            'description', 'notes', 'status', 'due_at', 'wake_at', 'followup_seconds', 'scope_json'}}
        row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise KeyError(f'待办 #{task_id} 不存在')
        if row['deleted_at'] is not None:
            raise ValueError('待办已删除')
        status = changes.get('status', row['status'])
        if status in ('done', 'cancelled'):
            changes['wake_at'] = None
            changes['completed_at'] = now if status == 'done' else None
        else:
            changes['completed_at'] = None
            if changes.get('wake_at', row['wake_at']) is None:
                changes['wake_at'] = now
        changes['updated_at'] = now
        sql = ', '.join(f'{column}=?' for column in changes)
        db.execute(f'UPDATE tasks SET {sql}, revision=revision+1 WHERE id=?',
                   (*changes.values(), task_id))
        db.execute("UPDATE reminders SET notification_status='superseded' "
                   "WHERE task_id=? AND notification_status IN ('pending', 'retry')", (task_id,))
        db.execute("UPDATE wechat_deliveries SET status='superseded' "
                   "WHERE reminder_id IN (SELECT id FROM reminders WHERE task_id=?) "
                   "AND status IN ('pending', 'retry')", (task_id,))

    def set_deleted(self, kind, item_id, deleted):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.set_deleted_in(db, kind, item_id, deleted, now=time.time())
        return self.get(item_id) if kind == 'task' else self.event(item_id)

    def set_deleted_in(self, db, kind, item_id, deleted, *, now):
        table = {'task': 'tasks', 'event': 'calendar_events'}[kind]
        row = db.execute(f'SELECT deleted_at FROM {table} WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise KeyError('事项不存在')
        if (row['deleted_at'] is not None) != deleted:
            db.execute(f'UPDATE {table} SET deleted_at=?,updated_at=?,revision=revision+1 WHERE id=?',
                       (now if deleted else None, now, item_id))
            if deleted and kind == 'task':
                db.execute("UPDATE executions SET stop_requested=1 WHERE task_id=? "
                           "AND status IN ('queued','running')", (item_id,))
                db.execute("UPDATE reminders SET notification_status='superseded' WHERE task_id=? "
                           "AND notification_status IN ('pending','retry')", (item_id,))
                db.execute("UPDATE wechat_deliveries SET status='superseded' "
                           "WHERE reminder_id IN (SELECT id FROM reminders WHERE task_id=?) "
                           "AND status IN ('pending','retry')", (item_id,))
            elif deleted:
                db.execute("UPDATE event_reminders SET status='superseded' WHERE event_id=? "
                           "AND status IN ('pending','retry')", (item_id,))

    def task_token(self, task_id):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO task_links VALUES (?, ?)',
                       (task_id, secrets.token_urlsafe(32)))
            return db.execute('SELECT token FROM task_links WHERE task_id=?', (task_id,)).fetchone()[0]

    def calendar_token_in(self, db):
        db.execute("INSERT OR IGNORE INTO settings VALUES ('calendar_token', ?)",
                   (secrets.token_urlsafe(32),))
        return db.execute("SELECT value FROM settings WHERE key='calendar_token'").fetchone()[0]

    def calendar_token(self):
        with self.connect() as db:
            return self.calendar_token_in(db)

    def authorize_calendar(self, token):
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM settings WHERE key='calendar_token' AND value=?",
                             (token,)).fetchone()
        if row is None:
            raise KeyError('日程链接不可用')

    def enqueue_execution(self, task_id, instruction):
        instruction = instruction.strip()
        if not instruction:
            raise ValueError('请填写希望助理推进的具体需求')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT status,deleted_at FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task['deleted_at'] is not None:
                raise ValueError('待办已删除')
            if task['status'] in ('done', 'cancelled'):
                raise ValueError('请先重新打开待办，再继续推进')
            if db.execute("SELECT 1 FROM executions WHERE task_id=? AND status IN ('queued','running')",
                          (task_id,)).fetchone():
                raise ValueError('任务正在推进，完成或停止后可补充要求继续')
            return db.execute('INSERT INTO executions (task_id,instruction,created_at) VALUES (?,?,?)',
                              (task_id, instruction, time.time())).lastrowid

    def stop_execution(self, task_id):
        with self.connect() as db:
            db.execute("UPDATE executions SET stop_requested=1 WHERE task_id=? "
                       "AND status IN ('queued','running')", (task_id,))

    def recover_executions(self):
        # Only called while this database's execution worker lock is held.
        with self.connect() as db:
            db.execute("UPDATE executions SET status='interrupted',finished_at=?, "
                       "error='服务中断，已保留进展；补充要求后可继续' WHERE status='running'", (time.time(),))

    def claim_execution(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE executions SET status='interrupted',finished_at=? "
                       "WHERE status='queued' AND (stop_requested=1 OR task_id IN "
                       "(SELECT id FROM tasks WHERE status IN ('done','cancelled')))", (time.time(),))
            row = db.execute("SELECT * FROM executions WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE executions SET status='running',started_at=? WHERE id=?",
                           (time.time(), row['id']))
        return dict(row) if row else None

    def execution_stopped(self, execution_id):
        with self.connect() as db:
            row = db.execute('SELECT e.stop_requested,t.status FROM executions e JOIN tasks t '
                             'ON t.id=e.task_id WHERE e.id=?', (execution_id,)).fetchone()
        return bool(row['stop_requested'] or row['status'] in ('done', 'cancelled'))

    def execution_event(self, execution_id, kind, text, *, plan=None):
        with self.connect() as db:
            db.execute('INSERT INTO execution_events (execution_id,kind,text,created_at) VALUES (?,?,?,?)',
                       (execution_id, kind, text, time.time()))
            if plan is not None:
                db.execute('UPDATE executions SET plan_json=? WHERE id=?', (encode(plan), execution_id))

    def executions(self, task_id):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM executions WHERE task_id=? ORDER BY id DESC LIMIT 10',
                              (task_id,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item['plan'] = json.loads(item.pop('plan_json'))
                item['events'] = [dict(event) for event in db.execute(
                    'SELECT kind,text,created_at FROM execution_events WHERE execution_id=? ORDER BY id',
                    (row['id'],))]
                result.append(item)
        return result

    def task_thread(self, task_id, thread_id=None):
        with self.connect() as db:
            if thread_id is not None:
                db.execute('INSERT INTO task_threads VALUES (?,?) ON CONFLICT(task_id) '
                           'DO UPDATE SET thread_id=excluded.thread_id', (task_id, thread_id))
            row = db.execute('SELECT thread_id FROM task_threads WHERE task_id=?', (task_id,)).fetchone()
        return row[0] if row else None

    def finish_execution(self, execution_id, *, status, result='', question='', error=None,
                         notify=False, wechat=None):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT e.*,t.description FROM executions e JOIN tasks t ON t.id=e.task_id '
                             'WHERE e.id=?', (execution_id,)).fetchone()
            if row['status'] != 'running':
                return
            db.execute('UPDATE executions SET status=?,result=?,question=?,error=?,finished_at=? WHERE id=?',
                       (status, result, question, error, now, execution_id))
            labels = dict(completed='本轮推进已完成，请查看结果。', needs_input='需要你补充信息，详情页可以直接回复。',
                          failed='这次推进未完成，请查看原因后继续。', interrupted='推进已停止，已有进展已保留。')
            # Reuse the existing inbox/outbox. Delivery never controls execution success.
            run_id = db.execute('INSERT INTO runs (task_id,trigger,context_json,created_at) VALUES (?,?,?,?)',
                                (row['task_id'], 'execution_' + status,
                                 encode(dict(execution_id=execution_id)), now)).lastrowid
            reminder_id = db.execute('INSERT INTO reminders '
                '(task_id,run_id,title,body,created_at,notification_status,retry_at) VALUES (?,?,?,?,?,?,?)',
                (row['task_id'], run_id, row['description'], labels[status], now,
                 'pending' if notify else 'disabled', now)).lastrowid
            if wechat:
                db.execute('INSERT INTO wechat_deliveries '
                           '(reminder_id,recipient_id,bot_id,client_id,retry_at) VALUES (?,?,?,?,?)',
                           (reminder_id, wechat.recipient_id, wechat.bot_id, 'pa:' + uuid4().hex, now))

    def linked_task(self, token):
        with self.connect() as db:
            row = db.execute('SELECT task_id FROM task_links WHERE token=?', (token,)).fetchone()
        if row is None:
            raise KeyError('待办链接不可用')
        return self.get(row[0])

    def wechat_state(self, wechat, *, now):
        key = 'wechat:' + wechat.bot_id + ':' + wechat.recipient_id
        with self.connect() as db:
            initial = dict(cursor=wechat.state().get('updatesBuf', ''), enabled_at=now, context=None)
            db.execute('INSERT OR IGNORE INTO settings VALUES (?, ?)', (key, encode(initial)))
            return json.loads(db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0])

    def consume_wechat(self, wechat, state, result, handler, *, now):
        # Applying an instruction, remembering its ID, and advancing the cursor are one commit.
        key = 'wechat:' + wechat.bot_id + ':' + wechat.recipient_id
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            renewed = False
            for msg in result.get('msgs') or []:
                if msg.get('message_type') != 1 or msg.get('from_user_id') != wechat.recipient_id:
                    continue
                if not msg.get('message_id') or msg.get('create_time_ms', 0) / 1000 < state['enabled_at']:
                    continue
                received_at = msg.get('create_time_ms', 0) / 1000
                if msg.get('context_token') and received_at > state.get('context_received_at', state['enabled_at']):
                    state['context'] = msg['context_token']
                    state['context_received_at'] = received_at
                    renewed = True
                identity = (wechat.bot_id, wechat.recipient_id, str(msg['message_id']))
                if db.execute('SELECT 1 FROM wechat_commands WHERE bot_id=? AND recipient_id=? AND message_id=?',
                              identity).fetchone():
                    continue
                handled = handler(db, msg, now)
                if handled is None:
                    continue
                task_id, command, reply = handled
                db.execute('INSERT INTO wechat_commands '
                           '(bot_id, recipient_id, message_id, task_id, command, reply, client_id, created_at, retry_at) '
                           'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (*identity, task_id, command, reply, 'pa:reply:' + uuid4().hex, now, now))
            if renewed:
                self.resume_wechat_in(db, wechat, now=now)
            state['cursor'] = result.get('get_updates_buf', state['cursor'])
            db.execute('UPDATE settings SET value=? WHERE key=?', (encode(state), key))
        return state

    def resume_wechat_in(self, db, wechat, *, now):
        """A fresh self-message renews only deliveries rejected for lack of a session."""
        target = (now, wechat.bot_id, wechat.recipient_id, *SESSION_ERRORS)
        db.execute("UPDATE wechat_deliveries SET status='pending',attempts=0,error=NULL,retry_at=? "
            "WHERE bot_id=? AND recipient_id=? AND error IN (?,?) AND status IN ('failed','retry') "
            "AND reminder_id IN (SELECT r.id FROM reminders r JOIN tasks t ON t.id=r.task_id "
            "WHERE r.read_at IS NULL AND t.deleted_at IS NULL AND t.status IN ('open','waiting'))", target)
        db.execute("UPDATE event_reminders SET status='pending',attempts=0,error=NULL,retry_at=? "
            "WHERE bot_id=? AND recipient_id=? AND error IN (?,?) AND status IN ('failed','retry') "
            "AND starts_at>? AND event_id IN (SELECT id FROM calendar_events "
            "WHERE status='confirmed' AND deleted_at IS NULL)", (*target, now))
        db.execute("UPDATE insights SET notification_status='pending',notification_attempts=0,"
            "notification_error=NULL,retry_at=? WHERE bot_id=? AND recipient_id=? "
            "AND notification_error IN (?,?) AND notification_status IN ('failed','retry') "
            "AND read_at IS NULL AND COALESCE(expires_at,created_at+86400)>?", (*target, now))
        row = db.execute("SELECT value FROM settings WHERE key='sources'").fetchone()
        timezone = (json.loads(row[0]) if row else default_sources())['timezone']
        day = datetime.fromtimestamp(now, ZoneInfo(timezone)).date().isoformat()
        db.execute("UPDATE briefing_deliveries SET status='pending',attempts=0,error=NULL,retry_at=? "
            "WHERE bot_id=? AND recipient_id=? AND error IN (?,?) AND status IN ('failed','retry') "
            "AND (kind='changes' OR day=?)", (*target, day))

    def claim_wechat_reply(self, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM wechat_commands WHERE status IN ('pending','retry') "
                             'AND retry_at<=? ORDER BY created_at, rowid LIMIT 1', (now,)).fetchone()
            if row:
                db.execute("UPDATE wechat_commands SET status='sending', attempts=attempts+1 WHERE client_id=?",
                           (row['client_id'],))
        return dict(row) if row else None

    def wechat_reply_result(self, client_id, *, status, error=None, now):
        with self.connect() as db:
            db.execute("UPDATE wechat_commands SET status=CASE WHEN ?='retry' AND attempts>=3 "
                       "THEN 'failed' ELSE ? END, error=?, retry_at=? WHERE client_id=? AND status='sending'",
                       (status, status, error, now + 60, client_id))

    def commit_check(self, task, *, observed, now, reminder=None, context=None,
                     trigger='', notify=False, wechat=None):
        """Save the next wake and inbox item together; competing ticks cannot duplicate it."""
        next_wake = task['wake_at']
        if reminder and next_wake is not None and next_wake <= now:
            next_wake = now + task['followup_seconds']
            if task['due_at'] is not None and task['due_at'] > now:
                next_wake = min(next_wake, task['due_at'])
        with self.connect() as db:
            cursor = db.execute("""
                UPDATE tasks SET observed_json=?, wake_at=?, revision=revision+1
                WHERE id=? AND revision=? AND status IN ('open', 'waiting') AND deleted_at IS NULL
            """, (None if observed is None else encode(observed), next_wake,
                  task['id'], task['revision']))
            if cursor.rowcount == 0:
                return None
            if not reminder:
                return None
            run_id = db.execute("INSERT INTO runs (task_id, trigger, context_json, created_at) "
                                "VALUES (?, ?, ?, ?)",
                                (task['id'], trigger, encode(context), now)).lastrowid
            reminder_id = db.execute("""
                INSERT INTO reminders (task_id, run_id, title, body, created_at,
                                       notification_status, retry_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (task['id'], run_id, reminder['title'], reminder['body'], now,
                  'pending' if notify else 'disabled', now)).lastrowid
            if wechat:
                db.execute("INSERT INTO wechat_deliveries "
                           "(reminder_id, recipient_id, bot_id, client_id, retry_at) VALUES (?, ?, ?, ?, ?)",
                           (reminder_id, wechat.recipient_id, wechat.bot_id, 'pa:' + uuid4().hex, now))
        return reminder_id

    def inbox(self, *, task_id=None, unread_only=False, limit=100):
        conditions, params = ['t.deleted_at IS NULL'], []
        if task_id is not None:
            conditions.append('r.task_id=?')
            params.append(task_id)
        if unread_only:
            conditions.append('r.read_at IS NULL')
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self.connect() as db:
            rows = db.execute("SELECT r.*, runs.trigger, runs.context_json, t.status AS task_status, "
                              "w.status AS wechat_status, w.attempts AS wechat_attempts, w.error AS wechat_error "
                              "FROM reminders r JOIN runs ON runs.id=r.run_id "
                              "JOIN tasks t ON t.id=r.task_id "
                              "LEFT JOIN wechat_deliveries w ON w.reminder_id=r.id" + where +
                              ' ORDER BY r.id DESC LIMIT ?', (*params, limit)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['context'] = json.loads(item.pop('context_json'))
            items.append(item)
        return items

    def read(self, reminder_id, *, now=None):
        with self.connect() as db:
            db.execute("UPDATE reminders SET read_at=?, notification_status=CASE "
                       "WHEN notification_status IN ('pending', 'retry') THEN 'superseded' "
                       "ELSE notification_status END WHERE id=?",
                       (time.time() if now is None else now, reminder_id))
            db.execute("UPDATE wechat_deliveries SET status='superseded' WHERE reminder_id=? "
                       "AND status IN ('pending', 'retry')", (reminder_id,))

    def recover_wechat(self):
        # Called under delivery_lock: no other sender can still own a 'sending' row.
        with self.connect() as db:
            db.execute("UPDATE wechat_deliveries SET status='unknown', "
                       "error='上次发送中进程退出，请查看微信；不会自动重发' WHERE status='sending'")
            db.execute("UPDATE wechat_commands SET status='unknown', "
                       "error='回执发送中断；指令已处理，不会重复执行' WHERE status='sending'")
            db.execute("UPDATE event_reminders SET status='unknown', "
                       "error='上次日程提醒发送中断，请查看微信；不会自动重发' WHERE status='sending'")
            db.execute("UPDATE insights SET notification_status='unknown', "
                       "notification_error='投递时服务中断，请查看微信' WHERE notification_status='sending'")
            db.execute("UPDATE briefing_deliveries SET status='unknown', "
                       "error='发送时服务中断，请查看微信；不会自动重发' WHERE status='sending'")

    def event_occurrences(self, start, end, *, include_uncertain=False):
        events = occurrences(self.events(), start, end, include_uncertain=include_uncertain)
        with self.connect() as db:
            reminders = {(r['event_id'], r['starts_at']): dict(r) for r in db.execute(
                'SELECT event_id,starts_at,status,error,sent_at FROM event_reminders')}
        return [dict(e, reminder=reminders.get((e['id'], e['starts_at']))) for e in events]

    def queue_event_reminders(self, wechat, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE event_reminders SET status='expired' "
                       "WHERE status IN ('pending','retry') AND starts_at<=?", (now,))
            series = [self.event_row(r) for r in db.execute("SELECT * FROM calendar_events "
                "WHERE status='confirmed' AND deleted_at IS NULL AND remind_before_minutes IS NOT NULL")]
            horizon = max((e['remind_before_minutes'] * 60 for e in series), default=0)
            for event in occurrences(series, now, now + horizon + 1):
                if (event['starts_at'] is None or event['starts_at'] <= now or
                        event['starts_at'] - event['remind_before_minutes'] * 60 > now):
                    continue
                db.execute("""
                    INSERT INTO event_reminders
                        (event_id,starts_at,remind_before_minutes,recipient_id,bot_id,client_id,retry_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id,starts_at) DO UPDATE SET
                        remind_before_minutes=excluded.remind_before_minutes,
                        status='pending', retry_at=excluded.retry_at, error=NULL
                    WHERE event_reminders.status='superseded'
                """, (event['id'], event['starts_at'], event['remind_before_minutes'],
                      wechat.recipient_id, wechat.bot_id, 'event-' + uuid4().hex, now, now))

    def claim_event_reminder(self, now):
        item = None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("""
                SELECT w.*,e.title,e.timezone,e.location FROM event_reminders w
                JOIN calendar_events e ON e.id=w.event_id
                WHERE w.status IN ('pending','retry') AND w.retry_at<=?
                  AND e.status='confirmed' AND e.deleted_at IS NULL AND w.starts_at>?
                  AND e.remind_before_minutes=w.remind_before_minutes
                  AND w.starts_at-w.remind_before_minutes*60<=?
                ORDER BY w.starts_at,w.id
            """, (now, now, now)).fetchall()
            for row in rows:
                event = self.event_row(db.execute('SELECT * FROM calendar_events WHERE id=?',
                                                  (row['event_id'],)).fetchone())
                if not any(e['starts_at'] == row['starts_at'] for e in
                           occurrences([event], row['starts_at'], row['starts_at'] + 1)):
                    db.execute("UPDATE event_reminders SET status='superseded' WHERE id=?", (row['id'],))
                    continue
                db.execute("UPDATE event_reminders SET status='sending',attempts=attempts+1 WHERE id=?",
                           (row['id'],))
                item = dict(row)
                break
        return item

    def event_reminder_result(self, reminder_id, *, status, error=None, message_id=None, now):
        with self.connect() as db:
            db.execute("""
                UPDATE event_reminders SET status=CASE WHEN ?='retry' AND attempts>=3
                    THEN 'failed' ELSE ? END, error=?, provider_message_id=?, retry_at=?, sent_at=?
                WHERE id=? AND status='sending'
            """, (status, status, error, message_id, now + 60,
                  now if status == 'delivered' else None, reminder_id))

    def claim_wechat(self, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""
                SELECT w.*, r.task_id, r.title, r.body FROM wechat_deliveries w
                JOIN reminders r ON r.id=w.reminder_id JOIN tasks t ON t.id=r.task_id
                WHERE w.status IN ('pending', 'retry') AND w.retry_at<=?
                  AND r.read_at IS NULL AND t.deleted_at IS NULL AND t.status IN ('open', 'waiting') ORDER BY r.id LIMIT 1
            """, (now,)).fetchone()
            if row:
                db.execute("UPDATE wechat_deliveries SET status='sending', attempts=attempts+1 "
                           "WHERE reminder_id=?", (row['reminder_id'],))
        return dict(row) if row else None

    def wechat_result(self, reminder_id, *, status, error=None, message_id=None, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("""
                UPDATE wechat_deliveries SET status=CASE WHEN ?='retry' AND attempts>=3
                    THEN 'failed' ELSE ? END, error=?, provider_message_id=?, retry_at=?
                WHERE reminder_id=? AND status='sending'
            """, (status, status, error, message_id, now + 60, reminder_id))

    def retry_wechat(self, reminder_id):
        with self.connect() as db:
            cursor = db.execute("""
                UPDATE wechat_deliveries SET status='pending', attempts=0, error=NULL, retry_at=?
                WHERE reminder_id=? AND status IN ('failed', 'retry') AND reminder_id IN (
                    SELECT r.id FROM reminders r JOIN tasks t ON t.id=r.task_id
                    WHERE r.read_at IS NULL AND t.deleted_at IS NULL AND t.status IN ('open', 'waiting'))
            """, (time.time(), reminder_id))
            if not cursor.rowcount:
                raise ValueError('仅可重试未读、未结束待办的失败推送；结果未知时请先查看微信')

    def pending_notifications(self, now):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT r.* FROM reminders r JOIN tasks t ON t.id=r.task_id
                WHERE r.notification_status IN ('pending', 'retry') AND r.retry_at<=?
                  AND r.read_at IS NULL AND t.deleted_at IS NULL AND t.status IN ('open', 'waiting') ORDER BY r.id LIMIT 10
            """, (now,))]

    def notification_result(self, reminder_id, *, error=None, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("""
                UPDATE reminders SET notification_attempts=notification_attempts+1,
                    notification_status=CASE WHEN ? IS NULL THEN 'delivered'
                        WHEN notification_attempts>=2 THEN 'failed' ELSE 'retry' END,
                    notification_error=?, retry_at=?
                WHERE id=? AND notification_status IN ('pending', 'retry')
            """, (error, error, now + 60, reminder_id))


def default_sources():
    home = Path.home()
    return dict(history_db=str(Path(os.environ.get('WECHAT_HISTORY_DB') or
                                   home / 'wechat-mac-rpa/data/db/chat_history.db').expanduser().resolve()),
                history_account='unspecified', history_source=None,
                dh_knowledge=[], rpa_memory=[], timezone='Asia/Shanghai')
