"""Compatibility entry point; application code lives in the root-level layers."""

if __name__ == '__main__':
    from cli import main

    raise SystemExit(main())

from importlib import import_module
import sys


# Preserve existing imports and patch targets without a second package tree.
for _name in (
    'inputs', 'inputs.wechat', 'inputs.sources', 'inputs.devices',
    'memory', 'memory.context', 'memory.store', 'memory.extraction',
    'memory.personalization', 'memory.fields',
    'presentation', 'presentation.push', 'presentation.web', 'presentation.task_web',
    'place_providers', 'place_providers.amap', 'place_providers.baidu',
    'place_providers.ctrip', 'place_providers.cache', 'place_providers.coordinates',
    'place_providers.matching',
):
    _module = import_module(_name)
    sys.modules[f'{__name__}.{_name}'] = _module
    if '.' not in _name:
        globals()[_name] = _module

for _legacy, _name in {
    'app': 'app',
    'cli': 'cli',
    'followup': 'followup',
    'execution': 'execution',
    'briefing': 'briefing',
    'proactive': 'proactive',
    'integrations': 'integrations',
    'maps': 'maps',
    'places': 'places',
    'wechat_commands': 'wechat_commands',
    'store': 'memory.store',
    'extraction': 'memory.extraction',
    'personalization': 'memory.personalization',
    'sources': 'inputs.sources',
    'devices': 'inputs.devices',
    'task_web': 'presentation.task_web',
    'web': 'presentation.web',
    'recurrence': 'common.recurrence',
}.items():
    _module = import_module(_name)
    sys.modules[f'{__name__}.{_legacy}'] = _module
    globals()[_legacy] = _module

from memory.store import TaskStore
from followup import FollowUp

__all__ = ['TaskStore', 'FollowUp']
