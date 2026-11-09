"""Reconcile complete structured calendar snapshots into the shared event store."""

import hashlib
import json
import math
from zoneinfo import ZoneInfo

from common.fields import event_fields


SOURCE = 'apple_calendar'


class CalendarMemory:
    def __init__(self, store):
        self.store = store

    def status(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='apple_calendar'").fetchone()
        return dict(enabled=False, calendar_ids=[]) | (json.loads(row[0]) if row else {})

    @staticmethod
    def save_in(db, value):
        db.execute("INSERT INTO settings VALUES ('apple_calendar', ?) "
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (json.dumps(value, ensure_ascii=False),))

    def save_status(self, value):
        with self.store.connect() as db:
            self.save_in(db, value)

    def apply(self, snapshot, *, start, end, now, calendar_ids=()):
        if snapshot.get('complete') is not True or snapshot.get('start') != start or snapshot.get('end') != end:
            raise ValueError('Incomplete calendar snapshot')
        calendars = {c['id']: c['name'] for c in snapshot['calendars']}
        selected = set(calendar_ids or calendars)
        if selected - calendars.keys():
            raise ValueError('Calendar account unavailable')
        incoming = {}
        for event in snapshot['events']:
            if event['calendar_id'] not in calendars:
                raise ValueError('Unknown calendar')
            if event['calendar_id'] not in selected:
                continue
            if not event['id']:
                raise ValueError('Missing event identity')
            # Series ID + original occurrence start survives moving a single recurrence.
            identity = [event['calendar_id'], event['id'], event['occurrence']]
            key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            tz = event['timezone']
            ZoneInfo(tz)
            if not all(isinstance(event[k], (int, float)) and math.isfinite(event[k])
                       for k in ('starts_at', 'ends_at')):
                raise ValueError('Invalid calendar time')
            if event['ends_at'] < event['starts_at']:
                raise ValueError('Invalid calendar duration')
            if not (event['starts_at'] < end and max(event['ends_at'], event['starts_at'] + .001) > start):
                raise ValueError('Event outside snapshot')
            data = {k: event[k] for k in ('title', 'notes', 'location', 'starts_at',
                'ends_at', 'all_day', 'status', 'remind_before_minutes')}
            if data['ends_at'] == data['starts_at']:
                data['ends_at'] = None
            fields = event_fields(data, tz)
            # EventKit permits point events. Preserve their zero duration rather than reserving a whole day.
            fields['ends_at'] = event['ends_at']
            fields.update(timezone=tz, recurrence=None)
            scope = dict(source_kind=SOURCE, calendar_key=key, calendar_id=event['calendar_id'],
                         calendar_name=calendars[event['calendar_id']])
            if key in incoming:
                raise ValueError('Duplicate calendar identity')
            incoming[key] = (fields, scope)
        counts = dict(created=0, updated=0, cancelled=0)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = {e['scope']['calendar_key']: e for e in (self.store.event_row(r) for r in db.execute(
                "SELECT * FROM calendar_events WHERE json_extract(scope_json,'$.source_kind')=?", (SOURCE,)))}
            # A missing account/calendar is an incomplete read, not mass deletion.
            expected = set(calendar_ids) if calendar_ids else {e['scope']['calendar_id'] for e in previous.values()}
            if expected - calendars.keys():
                raise ValueError('Calendar account unavailable')
            created_ids = []
            for key, (fields, scope) in incoming.items():
                old = previous.get(key)
                if old is None:
                    created_ids.append(self.store.create_event_in(db, **fields, scope=scope, now=now))
                    counts['created'] += 1
                elif old['deleted_at'] is None:
                    changes = {k: v for k, v in fields.items() if old[k] != v}
                    if old['scope'] != scope:
                        changes['scope_json'] = json.dumps(scope, ensure_ascii=False)
                    if changes:
                        self.store.update_event_in(db, old['id'], changes, now=now)
                        counts['updated'] += 1
            for key, old in previous.items():
                deselected = bool(calendar_ids) and old['scope']['calendar_id'] not in selected
                missing = (key not in incoming and old['scope']['calendar_id'] in selected
                           and old['starts_at'] < end and max(old['ends_at'], old['starts_at'] + .001) > start)
                if (old['deleted_at'] is None and old['status'] != 'cancelled' and (deselected or missing)):
                    self.store.update_event_in(db, old['id'], dict(status='cancelled'), now=now)
                    counts['cancelled'] += 1
            # First import establishes a baseline for these records only, preserving pending user changes.
            config = self.status_in(db)
            if not config.get('last_success_at') and created_ids:
                from briefing import Briefings
                monitor = Briefings.read_setting(db, 'briefing_monitor', None)
                if monitor:
                    snapshot_now = Briefings(self.store).snapshot_in(db)
                    for event_id in created_ids:
                        key = f'event:{event_id}'
                        monitor['snapshot'][key] = snapshot_now[key]
                    Briefings.write_setting(db, 'briefing_monitor', monitor)
        return counts

    @staticmethod
    def status_in(db):
        row = db.execute("SELECT value FROM settings WHERE key='apple_calendar'").fetchone()
        return json.loads(row[0]) if row else {}
