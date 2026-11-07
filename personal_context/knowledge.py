"""Current knowledge snapshots shared by assistants, without a model runtime."""

from dataclasses import dataclass
import json

from common.history import SourceBinding


@dataclass(frozen=True)
class KnowledgeRef:
    source: SourceBinding
    document_id: str
    record_id: str


@dataclass(frozen=True)
class KnowledgeEvidence:
    ref: KnowledgeRef
    locator: dict


@dataclass(frozen=True)
class KnowledgeEntity:
    ref: KnowledgeRef
    kind: str
    name: str
    account: str | None
    identity_status: str
    fields: dict
    evidence: tuple[KnowledgeEvidence, ...] = ()


@dataclass(frozen=True)
class KnowledgeFact:
    ref: KnowledgeRef
    kind: str
    entity_refs: tuple[KnowledgeRef, ...]
    fields: dict
    evidence: tuple[KnowledgeEvidence, ...]
    authority: str = "extracted"


@dataclass(frozen=True)
class KnowledgeDocument:
    ref: KnowledgeRef
    title: str
    kind: str
    format: str
    text: str
    entity_refs: tuple[KnowledgeRef, ...]


def _contains(query, value):
    return not query or query.casefold() in json.dumps(value, ensure_ascii=False).casefold()


class KnowledgeReader:
    """Read one loaded snapshot; references never merge people by display name.

    Search is literal substring matching. Account selection follows only native
    exact_account identities. This is not DH's historical per-case admission.
    """

    def __init__(self, *, entities=(), facts=(), documents=()):
        self._entities = tuple(entities)
        # Preserve native order within each authority level.
        priorities = {"manual_correction": 0, "manual_fact": 1, "extracted": 2}
        self._facts = tuple(sorted(facts, key=lambda fact: priorities[fact.authority]))
        self._documents = tuple(documents)
        self._by_ref = {entity.ref: entity for entity in self._entities}

    def people(self, *, query=None, account=None, limit=20):
        return tuple(entity for entity in self._entities
                     if entity.kind == "person" and self._matches_account(entity, account)
                     and _contains(query, [entity.name, entity.fields]))[:limit]

    def facts(self, *, query=None, entity=None, account=None, limit=20):
        return tuple(fact for fact in self._facts
                     if self._matches_entities(fact.entity_refs, entity, account)
                     and _contains(query, [fact.fields, self._names(fact.entity_refs)]))[:limit]

    def documents(self, *, query=None, entity=None, limit=20):
        return tuple(doc for doc in self._documents
                     if (entity is None or entity in doc.entity_refs)
                     and _contains(query, [doc.title, doc.text, self._names(doc.entity_refs)]))[:limit]

    def _names(self, refs):
        return [[self._by_ref[ref].name, self._by_ref[ref].fields]
                for ref in refs if ref in self._by_ref]

    @staticmethod
    def _matches_account(entity, account):
        return account is None or (entity.identity_status == "exact_account"
                                   and entity.account == account)

    def _matches_entities(self, refs, entity, account):
        return ((entity is None or entity in refs)
                and (account is None or any(
                    ref in self._by_ref and self._matches_account(self._by_ref[ref], account)
                    for ref in refs)))

    def summary(self):
        sources = dict.fromkeys(item.ref.source for item in
                                (*self._entities, *self._facts, *self._documents))
        return dict(usage="current_snapshot", sources=tuple(sources),
                    entities=len(self._entities),
                    people=sum(e.kind == "person" for e in self._entities),
                    facts=len(self._facts), documents=len(self._documents))


def combine_knowledge(*readers: KnowledgeReader) -> KnowledgeReader:
    """Combine explicitly selected sources without rewriting their references."""
    return KnowledgeReader(entities=(e for r in readers for e in r._entities),
                           facts=(f for r in readers for f in r._facts),
                           documents=(d for r in readers for d in r._documents))
