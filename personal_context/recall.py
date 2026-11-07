"""Bounded lexical recall over current facts; no model call or extra database."""

from collections import Counter
import json
import math
import re


MEMORY_FIELDS = ('memory_id', 'origin', 'profile_id', 'dh_fact_id', 'category', 'field',
    'value', 'context', 'temporal_kind', 'time_scope', 'observed_at', 'valid_from', 'valid_until')


def tokens(text):
    words = re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+', text.casefold())
    return [part for word in words for part in
            ([word] if word.isascii() or len(word) == 1 else
             [word[i:i+2] for i in range(len(word)-1)])]


def recall(memories, query='', *, categories=(), limit=16, max_chars=6000):
    """Rank relevant facts while retaining dietary constraints as whole statements.

    Source filtering and temporal validity belong to the current-memory reader.
    This is lexical BM25 plus field relevance, not semantic/vector retrieval.
    """
    if not memories:
        return []
    documents = [Counter(tokens(' '.join(str(m.get(k) or '') for k in ('value', 'context'))))
                 for m in memories]
    frequencies = Counter(term for doc in documents for term in doc)
    average = sum(sum(doc.values()) for doc in documents) / len(documents) or 1
    terms = set(tokens(query))
    ranked = []
    for index, (memory, doc) in enumerate(zip(memories, documents)):
        score = 0.
        length = sum(doc.values())
        for term in terms.intersection(doc):
            frequency = doc[term]
            idf = math.log(1 + (len(documents) - frequencies[term] + .5) / (frequencies[term] + .5))
            score += idf * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / average))
        field = memory.get('field', '')
        dietary = 'restaurant' in categories and field in ('dietary_preference', 'food_preference')
        score += 2 if dietary else 1 if field == 'interest' and 'exhibition' in categories else 0
        if memory.get('temporal_kind') == 'historical':
            score *= .3
        ranked.append((dietary, score, memory.get('observed_at') or 0, -index, memory))
    result, size = [], 0
    for *_, memory in sorted(ranked, key=lambda r: r[:4], reverse=True):
        cost = len(json.dumps({k: memory[k] for k in MEMORY_FIELDS if k in memory}, ensure_ascii=False))
        if size + cost > max_chars:
            continue  # Never cut a fact in half and lose its qualification/negation.
        result.append(memory)
        size += cost
        if len(result) >= limit:
            break
    return result
