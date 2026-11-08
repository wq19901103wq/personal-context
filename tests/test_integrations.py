"""Independent external capabilities, native MCP config, and official API units."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from personal_assistant.cli import main
from personal_assistant.execution import build_runtime
from personal_assistant.integrations import Integrations
from personal_assistant.maps import PlaceMap
from personal_assistant.place_providers.amap import Amap
from personal_assistant.place_providers.coordinates import gcj02_to_wgs84, wgs84_to_gcj02
from personal_assistant.proactive import ProactiveWorker
from personal_assistant.store import TaskStore


class IntegrationsTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def configure(self, config):
        (self.root / 'integrations.json').write_text(json.dumps(config))
        return Integrations(self.root)

    def test_official_map_and_independent_reviews_are_selected_without_printing_secrets(self):
        integrations = self.configure(dict(amap_api_key='private-key', mcp_servers=dict(
            dianping=dict(command='node', args=['/private/tool.js'],
                env=dict(TOKEN='private-token'), enabled_tools=['search_places', 'get_place_details']),
            disabled=dict(url='https://example.test/?token=private-token', enabled=False),
            unscoped=dict(command='merchant-tool'))))
        providers = integrations.place_providers(self.root / 'cache')
        self.assertIsInstance(providers['places'], Amap)
        self.assertIs(providers['routes'], providers['places'])
        self.assertEqual([p.name for p in providers['reviews']], ['ctrip_web', 'baidu_web'])
        self.assertEqual(list(integrations.mcp_servers()), ['dianping'])
        self.assertEqual(integrations.status()['mcp_servers']['unscoped']['state'], 'needs_enabled_tools')
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['--db', str(self.root/'not-created.db'), 'integrations']), 0)
        self.assertNotIn('private-key', out.getvalue())
        self.assertNotIn('private-token', out.getvalue())
        self.assertNotIn('/private/tool.js', out.getvalue())
        self.assertFalse((self.root/'not-created.db').exists())

    def test_no_key_uses_explicit_web_fallback_and_reviews_are_not_map_ratings(self):
        integrations = Integrations(self.root)
        self.assertEqual(integrations.provider_names(), dict(
            places='baidu_web', routes='baidu_web', reviews=['ctrip_web', 'baidu_web']))
        integrations = self.configure(dict(amap_api_key='key', place_providers=dict(reviews='amap')))
        with self.assertRaisesRegex(ValueError, '总分不是评论'):
            integrations.place_providers(self.root/'cache')
        integrations = self.configure(dict(place_providers=dict(routes='none', reviews='none')))
        self.assertIsNone(integrations.place_providers(self.root/'cache')['reviews'])

    def test_native_mcp_is_passed_only_to_recommendation_runtime(self):
        integrations = self.configure(dict(mcp_servers=dict(dianping=dict(
            command='node', args=['/tool/dist/index.js'], enabled_tools=['check_session']))))
        store = TaskStore(self.root/'assistant.db')
        with patch('personal_assistant.execution.CodexRuntime') as factory:
            worker = ProactiveWorker(store)
            worker.runtime_factory()
            config = factory.call_args.kwargs['thread_config']
            self.assertEqual(config['mcp_servers'], integrations.mcp_servers())
            self.assertEqual(config['model'], 'gpt-6-luna')
            # Other callers keep their original tool boundary.
            build_runtime(store, '提取聊天')
            self.assertEqual(factory.call_args.kwargs['thread_config']['mcp_servers'], {})

    def test_amap_official_api_converts_coordinates_units_and_caches_without_secrets(self):
        lat, lon = wgs84_to_gcj02(30., 120.)
        raw = dict(id='amap-id', name='餐厅(东店)', location=f'{lon},{lat}',
                   pname='某省', cityname='某市', adname='某区', address='东路8号',
                   business=dict(rating='4.6', cost='80'))
        def response(endpoint, params):
            self.assertEqual(params['key'], 'private-key')
            if endpoint.endswith('direction/walking'):
                self.assertEqual(params['show_fields'], 'cost')
                return dict(status='1', route=dict(paths=[dict(distance='866', cost=dict(duration='818'))]))
            return dict(status='1', pois=[raw])
        fetch = Mock(side_effect=response)
        provider = Amap(self.root/'cache', 'private-key', fetch=fetch)
        point = provider.search('餐厅(东店)', '东路8号')[0]
        self.assertAlmostEqual(point['coordinates']['latitude'], 30., places=5)
        self.assertEqual(point['reviews'], [])  # Rating is not a review sample.
        origin = dict(name='行程地点', coordinates=dict(latitude=30.01, longitude=120., system='wgs84'))
        provider.nearby(origin, max_km=3, page=1)
        self.assertEqual(fetch.call_args.args[1]['page_num'], 2)
        self.assertEqual(fetch.call_args.args[1]['radius'], 3000)
        route = provider.walking_route(origin, point)
        self.assertEqual(route['distance_km'], .87)
        self.assertEqual(route['duration_minutes'], 14)
        self.assertNotIn('private-key', route['source_url'])
        self.assertEqual(provider.walking_route(origin, point), route)
        self.assertEqual(fetch.call_count, 3)
        for path in (self.root/'cache').glob('*.json'):
            self.assertNotIn('private-key', path.read_text())
        # Coordinates outside China do not receive an offset.
        self.assertEqual(gcj02_to_wgs84(51., 0.), (51., 0.))

    def test_amap_rejection_does_not_become_a_successful_result(self):
        fetch = Mock(return_value=dict(status='0', info='INVALID_USER_KEY', infocode='10001'))
        provider = Amap(self.root/'cache', 'invalid-key', fetch=fetch)
        self.assertEqual(provider.search('餐厅'), [])
        self.assertEqual(provider.search('餐厅'), [])
        self.assertEqual(fetch.call_count, 1)

    def test_independent_review_and_route_providers_match_their_own_branch_ids(self):
        point = dict(provider='maps', uid='map-1', name='餐厅(东店)', address='东路8号',
                     coordinates=dict(latitude=30.01, longitude=120., system='wgs84'))
        wrong = dict(point, name='餐厅(西店)', address='西路1号', uid='review-wrong')
        review_point = dict(point, provider='reviews', uid='review-east')
        route_point = dict(point, provider='routes', uid='route-east')
        places = SimpleNamespace(name='maps', nearby=Mock(return_value=[point]))
        reviews = SimpleNamespace(name='reviews', search=Mock(return_value=[wrong, review_point]),
                                  reviews=Mock(return_value=[dict(platform='大众点评', summary='现炒好吃')]))
        routes = SimpleNamespace(name='routes', search=Mock(return_value=[route_point]),
                                 walking_route=Mock(return_value=dict(duration_minutes=10)))
        maps = PlaceMap(self.root/'cache', providers=dict(places=places, routes=routes, reviews=reviews))
        origins = [dict(ref='device', name='当前位置',
                        coordinates=dict(latitude=30., longitude=120., system='wgs84'))]
        result = maps.discover(origins)
        self.assertEqual(result['nearby'][0]['reviews'][0]['platform'], '大众点评')
        self.assertEqual(reviews.reviews.call_args.args[0]['uid'], 'review-east')
        self.assertEqual(routes.walking_route.call_args.args[1]['uid'], 'route-east')
        reviews.search.return_value = [wrong]
        self.assertEqual(maps.discover(origins)['nearby'][0]['reviews'], [])
        self.assertEqual(reviews.reviews.call_count, 1)


if __name__ == '__main__':
    unittest.main()
