"""External capability configuration shared by recommendation providers and runtime.

API adapters return normalized data; MCP/CLI execution stays in the native runtime.
Private configuration lives beside the database, outside version control.
"""

import json
import os
from pathlib import Path

from place_providers.amap import Amap
from place_providers.baidu import BaiduMaps
from place_providers.ctrip import CtripReviews


class Integrations:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.path = Path(os.environ.get('PERSONAL_ASSISTANT_INTEGRATIONS',
                                       self.data_dir / 'integrations.json'))
        try:
            self.config = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.config = {}
        self.amap_key = os.environ.get('AMAP_MAPS_API_KEY') or self.config.get('amap_api_key', '')

    def provider_names(self):
        configured = self.config.get('place_providers', {})
        default = 'amap' if self.amap_key else 'baidu_web'
        return dict(places=configured.get('places', default),
                    routes=configured.get('routes', default),
                    reviews=configured.get('reviews', ['ctrip_web', 'baidu_web']))

    def place_providers(self, cache_dir):
        # Reuse adapter instances; cross-platform IDs never pass between adapters.
        providers = dict(baidu_web=BaiduMaps(cache_dir), ctrip_web=CtripReviews(cache_dir), none=None)
        if self.amap_key:
            providers['amap'] = Amap(cache_dir, self.amap_key)
        names = self.provider_names()
        selected = {}
        for capability, choice in names.items():
            choices = choice if isinstance(choice, list) else [choice]
            for name in choices:
                if name not in providers or (capability == 'places' and name == 'none'):
                    raise ValueError(f'未配置可用的 {capability} 数据接口：{name}')
                if capability == 'reviews' and name == 'amap':
                    raise ValueError('高德地点总分不是评论接口；评价请配置独立来源或 MCP')
                if capability != 'reviews' and (isinstance(choice, list) or name == 'ctrip_web'):
                    raise ValueError(f'{capability} 需要单个地点或路线接口')
            selected[capability] = ([providers[name] for name in choices if providers[name] is not None]
                                    if isinstance(choice, list) else providers[choice])
        return selected

    def mcp_servers(self):
        # enabled_tools is the explicit read-only tool set for background research.
        # Do not inherit unrelated account tools or a merchant server's write tools.
        return {name: dict(config, required=False) for name, config in
                self.config.get('mcp_servers', {}).items()
                if config.get('enabled', True) and config.get('enabled_tools')}

    def status(self):
        """Safe to print or provide to the model: no URL, arguments, env or keys."""
        return dict(place_providers=self.provider_names(),
            amap_configured=bool(self.amap_key),
            mcp_servers={name: dict(
                state=('disabled' if not config.get('enabled', True) else
                       'configured' if config.get('enabled_tools') else 'needs_enabled_tools'),
                enabled_tools=config.get('enabled_tools', []))
                for name, config in self.config.get('mcp_servers', {}).items()})
