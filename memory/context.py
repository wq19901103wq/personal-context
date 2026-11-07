"""Bound knowledge snapshots, independent of any display or push channel."""

from dataclasses import asdict

from personal_context import (open_dh_knowledge, open_rpa_knowledge,
                              open_assistant_knowledge, combine_knowledge)
from inputs.wechat import SOURCE_ERRORS


def knowledge_context(scope):
    query, account = scope.get('knowledge_query'), scope.get('person_account')
    result = dict(usage='current_snapshot', people=[], facts=[], documents=[], errors=[])
    if not query and not account:
        return result
    readers = []
    if scope.get('assistant_knowledge'):
        try:
            readers.append(open_assistant_knowledge(scope['assistant_knowledge']))
        except SOURCE_ERRORS as exc:
            result['errors'].append(dict(source='个人画像', error=str(exc)))
    for key, opener in (('dh_knowledge', open_dh_knowledge), ('rpa_memory', open_rpa_knowledge)):
        for path in scope.get(key, []):
            try:
                options = dict(account_id=scope['history_account']) if key == 'rpa_memory' else {}
                readers.append(opener(path, **options))
            except SOURCE_ERRORS as exc:
                result['errors'].append(dict(source=path, error=str(exc)))
    reader = combine_knowledge(*readers)
    result['people'] = [asdict(item) for item in reader.people(query=query, account=account, limit=5)]
    result['facts'] = [asdict(item) for item in reader.facts(query=query, account=account, limit=8)]
    if query:
        result['documents'] = [asdict(item) for item in reader.documents(query=query, limit=3)]
    return result
