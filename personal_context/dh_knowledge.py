"""Read native DH Wiki files; leave DH admission and frozen runtimes in DH."""

import json
from pathlib import Path

from common.history import SourceBinding
from .knowledge import (
    KnowledgeEntity, KnowledgeEvidence, KnowledgeFact, KnowledgeReader, KnowledgeRef,
)


_TABLES = {"attributes": "attribute", "relations": "relation",
           "events": "event", "addresses": "address"}


def open_dh_knowledge(path: str | Path, *, source_id: str | None = None) -> KnowledgeReader:
    """Read an explicit Wiki/library JSON or Share directory as a current snapshot.

    No automatic Share selection, history scan, source hash recomputation, model
    imports, or change to the generator's configured knowledge version.
    """
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        content = path / "content"
        path = (content / "library.json" if (content / "library.json").exists()
                else content / "knowledge.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["schema"] == "source_bound_wiki_library_v1":
        pages = ((page["id"], page["knowledge"]) for page in data["pages"])
    elif data["schema"] == "wiki_structured_v1":
        pages = (("knowledge", data),)
    else:
        raise ValueError(f"Unsupported DH knowledge format: {data['schema']}")
    entities, facts = [], []
    for page_id, knowledge in pages:
        source = SourceBinding(source_id or path.as_uri(), knowledge["subject"]["self_account"])

        def ref(record_id):
            return KnowledgeRef(source, page_id, str(record_id))

        # Preserve source locators and native reference IDs, without copying raw
        # messages or resolving them through the frozen experiment data layer.
        evidence = {key: KnowledgeEvidence(ref(key), value)
                    for key, value in knowledge.get("evidence", {}).items()}

        def evidence_for(record):
            return tuple(evidence[key] for key in record.get("evidence_refs", ()))

        for entity in knowledge["entities"]:
            entities.append(KnowledgeEntity(
                ref(entity["id"]), entity["kind"], entity["label"],
                entity.get("account") or None, entity["identity_status"],
                entity, evidence_for(entity),
            ))
        for table, kind in _TABLES.items():
            for record in knowledge.get(table, ()):
                ids = [record[key] for key in
                       ("subject_id", "object_id", "speaker_id", "target_id") if record.get(key)]
                ids.extend(p["entity_id"] for p in record.get("participants", ()))
                facts.append(KnowledgeFact(
                    ref(record["id"]), kind, tuple(ref(key) for key in dict.fromkeys(ids)),
                    record, evidence_for(record),
                ))
    return KnowledgeReader(entities=entities, facts=facts)
