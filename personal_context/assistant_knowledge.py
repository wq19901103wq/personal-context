"""Read assistant-derived profiles through the same knowledge API as DH and RPA."""

import hashlib
from pathlib import Path
import sqlite3

from common.history import SourceBinding
from .knowledge import KnowledgeEntity, KnowledgeEvidence, KnowledgeFact, KnowledgeReader, KnowledgeRef
from .memory import read_profiles


def open_assistant_knowledge(path: str | Path, *, now=None) -> KnowledgeReader:
    path = Path(path).expanduser().resolve()
    namespace = 'personal-assistant:' + hashlib.sha256(str(path).encode()).hexdigest()[:20]
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = read_profiles(db, now=now)
    finally:
        db.close()
    entities, facts = {}, []
    for row in rows:
        records = row['evidence']
        subject = records[0]
        source = SourceBinding(namespace, subject['source']['account_id'])
        entity_ref = KnowledgeRef(source, 'people', row['subject_key'])
        evidence = tuple(KnowledgeEvidence(
            KnowledgeRef(SourceBinding(**r['source']), r['stream_id'], r['id']),
            dict(kind=r['kind'], actor=r['actor'], time=r['time'], text=r['text'])) for r in records)
        if entity_ref not in entities:
            actor = subject['actor']
            account = (source.account_id if row['is_self'] else actor['source_sender_id'])
            exact = ((bool(row['is_self']) and source.account_id != 'unspecified') or
                     (not row['is_self'] and row['identity_basis'] == 'source_id' and bool(account)))
            entities[entity_ref] = KnowledgeEntity(
                entity_ref, 'person', row['subject_name'], account if exact else None,
                'exact_account' if exact else row['identity_basis'],
                dict(is_self=row['is_self'], subject_key=row['subject_key']), evidence)
        facts.append(KnowledgeFact(KnowledgeRef(source, 'profile_facts', str(row['id'])),
            row['category'], (entity_ref,), dict(text=row['value'], confidence=row['confidence'],
                field=row.get('field') or row['category'], observed_at=row.get('observed_at', row['created_at']),
                valid_from=row.get('valid_from'), valid_until=row.get('valid_until'),
                temporal_kind=row.get('temporal_kind', 'durable'), revision=row.get('revision', 0)), evidence))
    return KnowledgeReader(entities=entities.values(), facts=facts)
