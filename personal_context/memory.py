"""Current personal facts, shared by SQLite and read-only knowledge consumers."""

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def knowledge_paths(entries):
    paths = []
    for entry in entries:
        path = Path(entry).expanduser()
        if path.is_dir():
            content = path / 'content'
            path = content / ('library.json' if (content / 'library.json').exists() else 'knowledge.json')
        paths.append(path)
    return paths


def knowledge_signature(entries):
    """JSON-stable external source versions, shared by cache and delivery."""
    result = []
    for path in knowledge_paths(entries):
        try:
            stat = path.stat()
            result.append([str(path), stat.st_mtime_ns, stat.st_size])
        except FileNotFoundError:
            result.append([str(path), None, None])
    return result


def memory_time(value, timezone):
    """Read a stored timestamp or explicit date, without guessing relative text."""
    if value is None or value == '':
        return None
    if isinstance(value, (float, int)):
        return value
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(timezone))).timestamp()
    except ValueError:
        return None


def current_profile_sql():
    # A replacement can take effect in the future; the old version stays usable
    # until that boundary. Dismissed/retracted facts are never returned.
    return ("status IN ('active','superseded') AND temporal_kind!='historical' "
            'AND (valid_from IS NULL OR valid_from<=?) '
            'AND (valid_until IS NULL OR valid_until>?)')


def read_profiles(db, *, account_key=None, include_inactive=False, now=None):
    now = time.time() if now is None else now
    columns = {r[1] for r in db.execute('PRAGMA table_info(profile_facts)')}
    temporal = 'valid_until' in columns
    where, args = [], []
    if not include_inactive:
        where.append(current_profile_sql() if temporal else "status='active'")
        if temporal:
            args.extend((now, now))
    if account_key is not None:
        where.append('account_key=?')
        args.append(account_key)
    order = 'COALESCE(observed_at,created_at) DESC,id DESC' if temporal else 'id DESC'
    rows = db.execute('SELECT * FROM profile_facts' +
        (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY ' + order, args)
    items = []
    for row in rows:
        item = dict(row)
        item['evidence'] = json.loads(item.pop('evidence_json'))
        items.append(item)
    return items
