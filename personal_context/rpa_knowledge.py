"""Read existing RPA Wiki/overrides without starting MemoryEngine or its worker."""

from dataclasses import replace
import json
from pathlib import Path

from common.history import SourceBinding
from .knowledge import (
    KnowledgeDocument, KnowledgeEntity, KnowledgeEvidence, KnowledgeFact,
    KnowledgeReader, KnowledgeRef,
)


def _pointer(*parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def open_rpa_knowledge(
    path: str | Path, *, account_id: str = "unspecified", source_id: str | None = None,
) -> KnowledgeReader:
    """Load only wiki/{users,groups,topics} and the three existing override files.

    Wiki text remains a document. Nicknames/aliases remain unconfirmed identities.
    As in RPA runtime retrieval, lines containing [待验证] are omitted in memory.
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    source = SourceBinding(source_id or root.as_uri(), account_id)
    entities, facts, documents = {}, [], []

    def locator(relative, pointer=""):
        return KnowledgeEvidence(KnowledgeRef(source, relative, pointer or "document"),
                                 dict(path=str(root / relative), json_pointer=pointer))

    def entity_ref(category, name, evidence, aliases=()):
        ref = KnowledgeRef(source, category, name)
        if ref not in entities:
            entities[ref] = KnowledgeEntity(
                ref, {"users": "person", "groups": "group", "topics": "topic"}.get(category, "unknown"),
                name, None, "display_name_placeholder", {"aliases": list(aliases)}, (evidence,),
            )
        else:
            entity = entities[ref]
            entities[ref] = replace(entity, evidence=entity.evidence + (evidence,))
        return ref

    def overrides(filename):
        path = root / "overrides" / filename
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    for name, cfg in overrides("aliases.json").get("users", {}).items():
        entity_ref("users", name, locator("overrides/aliases.json", _pointer("users", name)),
                   aliases=cfg.get("aliases", ()))

    for category in ("users", "groups", "topics"):
        for path in sorted((root / "wiki" / category).glob("*.md")):
            relative = path.relative_to(root).as_posix()
            entity = entity_ref(category, path.stem, locator(relative))
            text = path.read_text(encoding="utf-8")
            text = "\n".join(line for line in text.split("\n") if "[待验证]" not in line)
            documents.append(KnowledgeDocument(
                KnowledgeRef(source, relative, "document"), path.stem, "wiki", "markdown",
                text, (entity,),
            ))

    relative = "overrides/facts.json"
    for name, cfg in overrides("facts.json").get("users", {}).items():
        for i, record in enumerate(cfg.get("facts", ())):
            evidence = locator(relative, _pointer("users", name, "facts", i))
            entity = entity_ref("users", name, evidence)
            facts.append(KnowledgeFact(evidence.ref, "manual_fact", (entity,), record,
                                       (evidence,), "manual_fact"))

    relative = "overrides/corrections.json"
    for category, entries in overrides("corrections.json").items():
        if category not in ("groups", "entities"):
            continue
        for name, cfg in entries.items():
            # Only an exact existing user key associates a correction with a
            # person. Group suffixes and aliases do not establish identity.
            entity_category = ("users" if category == "entities"
                               and KnowledgeRef(source, "users", name) in entities else category)
            for i, text in enumerate(cfg.get("corrections", ())):
                evidence = locator(relative, _pointer(category, name, "corrections", i))
                entity = entity_ref(entity_category, name, evidence)
                facts.append(KnowledgeFact(evidence.ref, "correction", (entity,), {"text": text},
                                           (evidence,), "manual_correction"))
    return KnowledgeReader(entities=entities.values(), facts=facts, documents=documents)
