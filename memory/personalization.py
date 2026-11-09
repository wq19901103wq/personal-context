"""One current personal context for recommendation and future tool consumers."""

from dataclasses import asdict
import hashlib
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from personal_context.dh_knowledge import open_dh_knowledge
from personal_context.memory import memory_time, knowledge_paths, knowledge_signature
from common.recurrence import occurrences, interval


FIELDS = {'interest', 'interests', 'hobby', 'hobbies', 'preference', 'food_preference',
          'dietary_preference', 'dietary_behavior', 'favorite_food', 'favorite_artist',
          'workplace_location', 'residence_area', 'residence_city', 'living_city'}
LOCATION_FIELDS = {'workplace_location', 'residence_area', 'residence_city', 'living_city'}
SINGLE_FIELDS = {'workplace_location', 'residence_area', 'residence_city'}
FIELD_ALIASES = dict(interests='interest', hobby='interest', hobbies='interest',
    favorite_artist='interest', favorite_food='food_preference', dietary_behavior='dietary_preference',
    living_city='residence_city')


def next_recommendation_time(events, now, expires_at, timezone):
    """Next daytime instant outside calendar occupancy and travel buffers."""
    tz = ZoneInfo(timezone)
    day = datetime.fromtimestamp(now, tz).date()
    last = datetime.fromtimestamp(expires_at, tz).date()
    while day <= last:
        start = max(now, datetime.combine(day, time(10), tz).timestamp())
        end = min(expires_at, datetime.combine(day, time(21), tz).timestamp())
        if start < end:
            blocked = sorted((interval(e)[0]-1800, interval(e)[1]+1800) for e in
                occurrences(events, start-1800, end+1800, include_uncertain=True))
            for a, b in blocked:
                if b <= start:
                    continue
                if a > start:
                    break
                start = max(start, b)
            if start < end:
                return start
        day += timedelta(days=1)
    return None


class PersonalContext:
    def __init__(self, store):
        self.store = store
        self._signature, self._facts = None, []

    def dh_facts(self):
        entries = self.store.settings()['dh_knowledge']
        paths = knowledge_paths(entries)
        signature = knowledge_signature(entries)
        if signature == self._signature:
            return self._facts
        facts, seen = [], set()
        for path in paths:
            if not path.exists():
                continue
            reader = open_dh_knowledge(path)
            accounts = {s.account_id for s in reader.summary()['sources']}
            people = {p.ref for account in accounts for p in reader.people(account=account, limit=10000)}
            for fact in reader.facts(limit=100000):
                f = fact.fields
                # Being mentioned alongside the user is not being the subject.
                if (fact.kind != 'attribute' or f.get('mode') != 'self_report'
                        or f.get('polarity') != 'affirmed' or f.get('field') not in FIELDS
                        or not any(r in people and r.record_id == f.get('subject_id')
                                   for r in fact.entity_refs)):
                    continue
                key = fact.ref
                if key in seen:
                    continue
                seen.add(key)
                qualified_id = 'dh:' + hashlib.sha256(json.dumps(asdict(fact.ref),
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
                facts.append(dict(id=qualified_id, field=FIELD_ALIASES.get(f['field'], f['field']), value=f['value'],
                    context=f.get('attribution', ''), temporal_kind=f.get('temporal_kind'),
                    time_scope=f.get('time_scope'), observed_at=f.get('last_observed_at'),
                    valid_from=f.get('valid_from'), valid_until=f.get('valid_to'),
                    ref=asdict(fact.ref), evidence=[asdict(e) for e in fact.evidence]))
        self._signature, self._facts = signature, facts
        return self._facts

    def memories(self, now):
        timezone = self.store.settings()['timezone']
        memories = []
        for p in self.store.profiles(now=now):
            if p['is_self'] != 1 or p['confidence'] != 'explicit':
                continue
            field = p['field'] or p['category']
            memories.append(dict(memory_id=f"profile:{p['id']}", origin='chat', profile_id=p['id'],
                category=p['category'], field=FIELD_ALIASES.get(field, field), value=p['value'],
                confidence=p['confidence'], temporal_kind=p['temporal_kind'], observed_at=p['observed_at'],
                valid_from=p['valid_from'], valid_until=p['valid_until'], revision=p['revision']))
        for f in self.dh_facts():
            start, end = (memory_time(f[k], timezone) for k in ('valid_from', 'valid_until'))
            # Keep an unparseable dated statement out of the current snapshot.
            if any(f[k] and bound is None for k, bound in (('valid_from', start), ('valid_until', end))):
                continue
            if (start is not None and start > now) or (end is not None and end <= now):
                continue
            kind = f['temporal_kind']
            if kind == 'event' or (kind == 'historical' and f['field'] in LOCATION_FIELDS):
                continue
            category = 'address' if f['field'] in LOCATION_FIELDS else (
                'interest' if f['field'] == 'interest' else 'preference')
            memories.append(dict(f, memory_id=f['id'], origin='dh', dh_fact_id=f['id'], category=category,
                confidence='explicit', observed_at=memory_time(f['observed_at'], timezone),
                valid_from=start, valid_until=end, temporal_kind=kind or 'unknown'))
        memories.sort(key=lambda m: (m['temporal_kind'] != 'historical', m['observed_at'] or 0,
                                     m['origin'] == 'chat'), reverse=True)
        result, seen, single = [], set(), set()
        for memory in memories:
            field = memory['field']
            key = (field, memory['value'].strip().casefold())
            if key in seen or (field in SINGLE_FIELDS and field in single):
                continue
            seen.add(key)
            if field in SINGLE_FIELDS:
                single.add(field)
            result.append(memory)
        return result

    def snapshot(self, now):
        config = self.store.personalization()
        memories = self.memories(now)
        location = config.get('current_location')
        if location and not 0 <= now - location['observed_at'] < 12 * 3600:
            location = None
        return dict(requested_categories=config.get('categories', []),
                    current_location=location,
                    memories=memories,
                    historical_locations=[dict(m, id=m['memory_id']) for m in memories if m['category'] == 'address'],
                    dh_facts=[dict(m, id=m['dh_fact_id']) for m in memories if m['origin'] == 'dh'],
                    chat_profiles=[dict(m, id=m['profile_id']) for m in memories if m['origin'] == 'chat'],
                    location_note='历史地址仅作常用区域线索；行程目的地不是实时位置。')
