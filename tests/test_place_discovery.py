"""Business regressions for automatic place discovery; no live messages or personal fixtures."""

import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from personal_assistant.followup import FollowUp
from personal_assistant.maps import PlaceMap, match_place
from personal_assistant.place_providers.baidu import (BaiduMaps, COMMENT_URL,
    bd09_to_mercator, bd09_to_wgs84, mercator_to_bd09, parse_reviews, wgs84_to_bd09)
from personal_assistant.places import origin_options, select_candidates, straight_line_km
from personal_assistant.proactive import ProactiveWorker
from personal_assistant.store import TaskStore, recommendation_place_key
from personal_assistant.task_web import calendar_view


class PlaceDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.day = datetime.fromtimestamp(self.now, ZoneInfo('Asia/Shanghai')).date()
        self.context = dict(chat_profiles=[], dh_facts=[], historical_locations=[], current_location=None)
        self.trigger = dict(kind='travel_prepare', target_date=self.day.isoformat(),
            expires_at=self.now+86400, event=dict(id=8, revision=2, location='本次行程地点', notes='门牌由日程提供'))
        self.origins = origin_options(self.trigger, self.context)
        self.payload = dict(summary='找到候选门店', origin=dict(ref='event:8:2',
            name='本次行程地点', address='行程地址', coordinates=None), candidates=[dict(
                category='restaurant', title='候选门店', body='', reason='',
                url='https://places.example/branch/1', profile_ids=[], dh_fact_ids=[],
                restaurant=dict(address='门店具体地址', coordinates=None,
                    route=dict(distance_km=1.8, duration_minutes=12, mode='walking',
                               source_url='https://maps.example/route/1'),
                    reviews=[dict(platform='评价平台', source_url='https://reviews.example/branch/1',
                        review_date=self.day.isoformat(), summary='菜品好评，午间有排队反馈')],
                    attraction='现制特色菜值得尝试。', fit='离行程地点步行可达，适合作为用餐备选。',
                    tradeoff='午间可能排队。', recommended_dishes='特色菜', price=''))])

    def select(self, payload=None):
        return select_candidates(payload or self.payload, self.origins,
            target_date=self.trigger['target_date'], timezone='Asia/Shanghai', now=self.now)

    def location_reply(self, origin=None):
        return SimpleNamespace(status='completed', final_message=json.dumps(dict(
            summary='根据目标时段的日程选择地点',
            origin=dict(origin or self.payload['origin'], conditional=True))))

    def test_route_and_dated_reviews_build_explanation(self):
        result = self.select()
        item = result['items'][0]
        self.assertIn('步行约 12 分钟，路线约 1.8 公里', item['body'])
        self.assertIn(self.day.isoformat(), item['body'])
        self.assertIn('午间可能排队', item['reason'])
        self.assertEqual(len(item['discovery']['sources']), 2)
        self.assertNotIn('评价平台', item['body'])
        self.assertNotIn('来源', item['body'])

    def test_undated_old_and_future_reviews_do_not_become_recent(self):
        for value in ('近期', '', (self.day-timedelta(days=91)).isoformat(),
                      (self.day+timedelta(days=1)).isoformat()):
            with self.subTest(value=value):
                payload = deepcopy(self.payload)
                payload['candidates'][0]['restaurant']['reviews'][0]['review_date'] = value
                result = self.select(payload)
                self.assertEqual(len(result['items']), 1)
                self.assertFalse(any(r['recent'] for r in result['items'][0]['discovery']['reviews']))
        payload['candidates'][0]['restaurant']['reviews'] = []
        self.assertEqual(len(self.select(payload)['items']), 1)

    def test_coordinates_do_not_turn_into_road_time_or_cross_system_distance(self):
        a = dict(latitude=30., longitude=120., system='wgs84', source_url='https://maps.example/origin')
        b = dict(a, latitude=30.01, source_url='https://maps.example/place')
        self.assertIsNone(straight_line_km(a, dict(b, system='gcj02')))
        payload = deepcopy(self.payload)
        payload['origin']['coordinates'] = a
        payload['candidates'][0]['restaurant'].update(route=None, coordinates=b)
        result = self.select(payload)
        self.assertIn('直线约', result['items'][0]['body'])
        self.assertNotIn('分钟', result['items'][0]['body'])
        self.assertIsNone(result['items'][0]['discovery']['travel']['duration_minutes'])
        payload['candidates'][0]['restaurant']['route'] = dict(
            distance_km=None, duration_minutes=None, mode='walking', source_url='https://maps.example/route')
        self.assertEqual(len(self.select(payload)['items']), 1)
        payload['candidates'][0]['restaurant']['coordinates']['system'] = 'gcj02'
        self.assertFalse(self.select(payload)['items'])

    def test_unknown_origin_and_long_route_are_rejected(self):
        payload = deepcopy(self.payload)
        payload['origin']['ref'] = 'invented-origin'
        self.assertFalse(self.select(payload)['items'])
        payload = deepcopy(self.payload)
        payload['candidates'][0]['restaurant']['route']['duration_minutes'] = 70
        self.assertIn('交通时间', self.select(payload)['rejected'][0]['reason'])
        historical = dict(self.context, historical_locations=[dict(id='fact-1', value='旧常用区域')])
        origins = origin_options(dict(kind='free_prepare'), historical)
        self.assertTrue(origins[0]['conditional'])

    @staticmethod
    def poi(name, address, latitude, longitude):
        x, y = bd09_to_mercator(*wgs84_to_bd09(latitude, longitude))
        return dict(name=name, addr=address, uid=name, city_id=123,
            geo=f'1|{x},{y};{x},{y}|', ext=json.dumps(dict(detail_info=dict(
                tag='餐馆', gd_rcmd_dish=dict(featured_menus=[dict(name='特色菜')]),
                comments=[dict(cn_name='地图评价', content=base64.b64encode('菜品现做'.encode()).decode(),
                               date='')]))))

    def test_map_projection_device_position_radius_and_cache(self):
        # Public coordinate example, independent of any personal itinerary.
        bd_lat, bd_lon = wgs84_to_bd09(39.908823, 116.397470)
        self.assertAlmostEqual(bd_lat, 39.916565, places=4)
        self.assertAlmostEqual(bd_lon, 116.410086, places=4)
        lat, lon = bd09_to_wgs84(bd_lat, bd_lon)
        self.assertAlmostEqual(lat, 39.908823, places=5)
        self.assertAlmostEqual(lon, 116.397470, places=5)
        self.assertAlmostEqual(mercator_to_bd09(*bd09_to_mercator(bd_lat, bd_lon))[0], bd_lat, places=6)
        def response(endpoint, params):
            if endpoint == COMMENT_URL:
                return dict(data=dict(comment_list=[]))
            if params['qt'] == 'walk':
                return dict(content=dict(routes=[]))
            return dict(content=[self.poi('近店', '一号路1号', 30.01, 120),
                                 self.poi('远店', '二号路2号', 30.1, 120)])
        fetch = Mock(side_effect=response)
        origins = origin_options(dict(kind='free_time'), dict(current_location=dict(latitude=30., longitude=120.)))
        with TemporaryDirectory() as directory:
            maps = PlaceMap(directory, provider=BaiduMaps(directory, fetch=fetch))
            result = maps.discover(origins)
            self.assertEqual([p['name'] for p in result['nearby']], ['近店'])
            self.assertAlmostEqual(result['nearby'][0]['distance_km'], 1.11, places=2)
            self.assertIn('nb_x', fetch.call_args_list[0].args[1])
            self.assertNotIn('center', fetch.call_args_list[0].args[1])
            self.assertEqual(result['nearby'][0]['reviews'][0]['summary'], '菜品现做')
            maps.discover(origins)
            # The nearby query, two review tabs and unavailable route are all cached.
            self.assertEqual(fetch.call_count, 4)

    def test_map_matches_branch_and_address_instead_of_first_hit(self):
        places = [dict(name='好菜馆(城西店)', address='西路1号'),
                  dict(name='好菜馆(城东店)', address='东路8号')]
        self.assertEqual(match_place('好菜馆(城东店)', '东路8号', places), places[1])
        self.assertIsNone(match_place('好菜馆', '', places))
        self.assertIsNone(match_place('好菜馆(城东店)', '东路9号', places))

    def test_parenthetical_description_does_not_require_a_branch_alias(self):
        place = dict(name='好菜馆', address='东路8号')
        self.assertEqual(match_place('好菜馆(招牌)', '东路8号', [place]), place)

    def test_category_order_and_feedback_do_not_hide_valid_candidates(self):
        for categories in [('exhibition', 'restaurant'), ('exhibition', 'exhibition')]:
            with self.subTest(categories=categories), TemporaryDirectory() as directory:
                payload = deepcopy(self.payload)
                rejected = dict(category='exhibition', title='已拒绝的展览', body='展览内容',
                    reason='具体特色', url='https://places.example/rejected',
                    profile_ids=[], dh_fact_ids=[], restaurant=None)
                candidates = [rejected]
                for index, category in enumerate(categories):
                    candidate = deepcopy(self.payload['candidates'][0])
                    candidate.update(category=category, title=f'候选{index}',
                                     url=f'https://places.example/{index}')
                    if category == 'exhibition':
                        candidate.update(restaurant=None, body='有效展期与地点', reason='适合本次行程')
                    candidates.append(candidate)
                payload['candidates'] = candidates
                def reply(prompt, **kwargs):
                    return self.location_reply() if json.loads(prompt)['stage'] == 'resolve_location' else SimpleNamespace(
                        status='completed', final_message=json.dumps(payload))
                runtime = SimpleNamespace(create_thread=AsyncMock(), close=AsyncMock(),
                    run_turn=AsyncMock(side_effect=reply))
                maps = Mock()
                maps.discover.return_value = dict(origin=None, nearby=[])
                maps.enrich.return_value = None
                store = TaskStore(Path(directory)/'assistant.db')
                store.rejected_recommendations = Mock(return_value=[dict(title=rejected['title'], address='',
                    place_key=recommendation_place_key(rejected['title'], {}))])
                worker = ProactiveWorker(store, runtime_factory=lambda: runtime, maps=maps)
                output = asyncio.run(worker.research(dict(timezone='Asia/Shanghai',
                    place_policy={}, recent_recommendations=[]), self.trigger, self.context, Event(), self.now))
                self.assertEqual([i['title'] for i in output['items']], ['候选0', '候选1'])
                self.assertEqual(runtime.run_turn.await_count, 2)

    def test_empty_first_search_then_map_distance_persistence_mobile_and_delivery(self):
        self.discovery_delivery()

    def test_actual_route_and_reviews_reach_selection_mobile_and_wechat(self):
        self.discovery_delivery(route=dict(distance=750, duration=600))

    def discovery_delivery(self, route=None):
        invalid = deepcopy(self.payload)
        invalid['candidates'] = []
        valid = deepcopy(self.payload)
        valid['candidates'][0]['restaurant'].update(route=None, coordinates=None, reviews=[])
        outputs, prompts = [invalid, valid], []
        location_reply = self.location_reply()

        class Runtime:
            async def create_thread(self):
                pass

            async def run_turn(self, prompt, **kwargs):
                prompt = json.loads(prompt)
                if prompt['stage'] == 'resolve_location':
                    return location_reply
                prompts.append(prompt)
                return SimpleNamespace(status='completed', final_message=json.dumps(outputs.pop(0)))

            async def close(self):
                pass

        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory)/'assistant.db')
            store.set_personalization(dict(enabled=True))
            def fetch(endpoint, params):
                if endpoint == COMMENT_URL:
                    return dict(data=dict(comment_list=[dict(src='baidumap', cn_name='百度地图',
                        content='特色菜现做' if params['tab'] == 5 else '高峰期需要排队',
                        date=self.day.isoformat(), poi_uid=params['uid'])]))
                if params['qt'] == 'walk':
                    return dict(content=dict(routes=[dict(legs=[route])] if route else []))
                return dict(content=[self.poi('本次行程地点', '行程地址', 30., 120.)] if params['qt'] == 's'
                    else [self.poi('候选门店', '门店具体地址', 30.01, 120.)])
            maps = PlaceMap(Path(directory)/'places', provider=BaiduMaps(Path(directory)/'places', fetch=fetch))
            worker = ProactiveWorker(store, runtime_factory=Runtime, maps=maps)
            prompt = dict(timezone='Asia/Shanghai', place_policy={}, recent_recommendations=[])
            output = asyncio.run(worker.research(prompt, self.trigger, self.context, Event(), self.now))
            self.assertEqual(len(prompts), 2)
            self.assertEqual(prompts[1]['map_places']['nearby'][0]['name'], '候选门店')
            nearby = prompts[1]['map_places']['nearby'][0]
            self.assertEqual(nearby['reviews'][0]['review_date'], self.day.isoformat())
            self.assertIn('高峰期需要排队', [r['summary'] for r in nearby['reviews']])
            self.assertEqual(bool(nearby['route']), bool(route))
            self.assertEqual(output['items'][0]['discovery']['travel']['kind'],
                             'route' if route else 'straight_line')
            versions = store.recommendation_context()
            self.assertTrue(store.start_proactive('run-one', self.trigger, versions,
                day=self.day.isoformat(), now=self.now))
            wechat = SimpleNamespace(recipient_id='self', bot_id='test-bot', send=Mock())
            ids = store.finish_proactive('run-one', self.trigger, versions, self.context,
                output, wechat=wechat, now=self.now)
            self.assertEqual(len(ids), 1)
            app = SimpleNamespace(store=store)
            self.assertEqual(calendar_view(app)['insights'][0]['discovery'], output['items'][0]['discovery'])
            with store.connect() as db:
                research = json.loads(db.execute('SELECT research_json FROM proactive_runs').fetchone()[0])
            self.assertEqual(len(research['rounds']), 2)
            followup = FollowUp(store, wechat=wechat)
            followup.send_insight(self.now, requested=True)
            text = wechat.send.call_args.args[0]
            self.assertIn('步行约 10 分钟，路线约 0.75 公里' if route else '直线约 1.1 公里', text)
            self.assertNotIn('https://reviews.example/branch/1', text)
            self.assertNotIn('交通来源', text)
            followup.send_insight(self.now+1, requested=True)
            self.assertEqual(wechat.send.call_count, 1)

    def test_reviews_decode_original_dates_and_exclude_generated_or_other_branches(self):
        escaped = lambda value: json.dumps(value, ensure_ascii=True)[1:-1]
        raw = [dict(src='baidumap', cn_name=escaped('百度地图'),
                    content=escaped('现炒好吃😀'), date=self.day.isoformat(),
                    poi_uid='branch', comment_jump_url=r'https:\/\/reviews.example\/one'),
               dict(src='aigc_renew', from_source='aigc', content='生成好评', date=self.day.isoformat()),
               dict(src='ugc', content='其他分店', poi_uid='another', date=self.day.isoformat()),
               dict(src='ugc', content='未来评论', date=(self.day+timedelta(days=1)).isoformat())]
        parsed = parse_reviews(raw, 'branch')
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['summary'], '现炒好吃😀')
        self.assertEqual(parsed[0]['platform'], '百度地图')
        self.assertEqual(parsed[0]['source_url'], 'https://reviews.example/one')
        old = parse_reviews([dict(content=base64.b64encode('排队较久'.encode()).decode(),
                                 date='', cn_name='大众点评')], 'branch', encoded=True)
        self.assertEqual(old[0]['summary'], '排队较久')
        self.assertEqual(old[0]['review_date'], '')

    def test_walking_route_adds_legs_and_caches_normalized_result(self):
        point = dict(name='出发点', coordinates=dict(latitude=30., longitude=120., system='wgs84'))
        destination = dict(name='门店', city_id=123, coordinates=dict(point['coordinates'], latitude=30.01))
        fetch = Mock(return_value=dict(content=dict(routes=[dict(legs=[
            dict(distance=500, duration=400), dict(distance=366, duration=418)])])))
        with TemporaryDirectory() as directory:
            provider = BaiduMaps(directory, fetch=fetch)
            route = provider.walking_route(point, destination)
            self.assertEqual(route['distance_km'], .87)
            self.assertEqual(route['duration_minutes'], 14)
            self.assertEqual(route['mode'], 'walking')
            self.assertEqual(provider.walking_route(point, destination), route)
            self.assertEqual(fetch.call_count, 1)
            cached = json.loads(next(Path(directory).glob('*.json')).read_text())['value']
            self.assertEqual(cached, route)

    def test_changed_origin_does_not_reuse_another_starting_points_route(self):
        nearby = dict(name='候选门店', address='门店具体地址', reviews=[],
            coordinates=dict(latitude=30.01, longitude=120., system='wgs84',
                             source_url='https://maps.example/place'),
            route=self.payload['candidates'][0]['restaurant']['route'])
        second = dict(ref='second-origin', name='另一处出发点',
                      coordinates=dict(latitude=30.02, longitude=120., system='wgs84', source_url=''),
                      basis='设备大致定位', conditional=False, approximate=True)
        payload = deepcopy(self.payload)
        payload['origin'].update(ref=second['ref'])
        provider = Mock()
        provider.walking_route.return_value = None
        maps = PlaceMap('', provider=provider)
        maps.enrich(payload, [*self.origins, second],
            dict(origin=dict(ref='event:8:2'), nearby=[nearby]))
        self.assertIsNone(payload['candidates'][0]['restaurant']['route'])
        self.assertEqual(provider.walking_route.call_args.args[0]['name'], second['name'])
        selected = select_candidates(payload, [second], target_date=self.day.isoformat(),
                                     timezone='Asia/Shanghai', now=self.now)
        self.assertIn('定位精度约 1 公里', selected['items'][0]['body'])

    def test_device_route_also_displays_approximate_position(self):
        origins = origin_options(dict(kind='free_time'),
            dict(current_location=dict(latitude=30., longitude=120.)))
        payload = deepcopy(self.payload)
        payload['origin']['ref'] = origins[0]['ref']
        selected = select_candidates(payload, origins, target_date=self.day.isoformat(),
                                     timezone='Asia/Shanghai', now=self.now)
        self.assertIn('步行约 12 分钟', selected['items'][0]['body'])
        self.assertIn('定位精度约 1 公里', selected['items'][0]['body'])

    def test_resolved_event_venue_drives_next_search_and_final_reasons(self):
        trigger = deepcopy(self.trigger)
        trigger['event'].update(title='出行住宿：新旅馆', location='目的地（会合点待定）',
                                notes='旧旅馆已取消，确认入住新旅馆；会合地点另定。')
        first = dict(summary='已确定住宿地点', origin=dict(ref='event:8:2',
            name='新旅馆', address='北路10号', coordinates=None), candidates=[])
        final = deepcopy(self.payload)
        final['origin'] = deepcopy(first['origin'])
        final['candidates'][0]['restaurant'].update(route=None, coordinates=None, reviews=[])
        final['candidates'][0]['restaurant']['tradeoff'] = '晚餐高峰可能排队。'
        prompts, nearby_pages = [], []
        location_reply = self.location_reply(first['origin'])

        class Runtime:
            async def create_thread(self):
                pass

            async def run_turn(self, prompt, **kwargs):
                prompt = json.loads(prompt)
                if prompt['stage'] == 'resolve_location':
                    prompts.append(prompt)
                    return location_reply
                prompts.append(prompt)
                return SimpleNamespace(status='completed',
                    final_message=json.dumps(final))

            async def close(self):
                pass

        def fetch(endpoint, params):
            if endpoint == COMMENT_URL:
                return dict(data=dict(comment_list=[dict(src='baidumap',
                    content='现做特色菜' if params['tab'] == 5 else '晚餐高峰排队',
                    date=self.day.isoformat(), poi_uid=params['uid'])]))
            if params['qt'] == 'walk':
                return dict(content=dict(routes=[dict(legs=[dict(distance=900, duration=720)])]))
            if params['qt'] == 'nb':
                self.assertEqual(params['c'], 123)
                nearby_pages.append(params['pn'])
                return dict(content=[self.poi('候选门店', '门店具体地址', 30.005, 120.)])
            return dict(content=[self.poi('新旅馆', '北路10号', 30., 120.)]
                        if '新旅馆' in params['wd'] else [])

        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory)/'assistant.db')
            maps = PlaceMap(directory, provider=BaiduMaps(directory, fetch=fetch))
            worker = ProactiveWorker(store, runtime_factory=Runtime, maps=maps)
            output = asyncio.run(worker.research(dict(timezone='Asia/Shanghai',
                place_policy={}, recent_recommendations=[]), trigger, self.context, Event(), self.now))
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[0]['origin_options'][0]['title'], trigger['event']['title'])
        self.assertEqual(nearby_pages, [0, 1])
        retrieved = prompts[1]['map_places']
        self.assertEqual(retrieved['origin']['name'], '新旅馆')
        self.assertEqual(retrieved['nearby'][0]['route']['duration_minutes'], 12)
        self.assertIn('晚餐高峰排队', [r['summary'] for r in retrieved['nearby'][0]['reviews']])
        self.assertIn('晚餐高峰可能排队', output['items'][0]['reason'])
        self.assertIn('从新旅馆步行约 12 分钟', output['items'][0]['body'])

    def test_details_already_shown_do_not_trigger_another_model_comparison(self):
        payload = deepcopy(self.payload)
        second = deepcopy(payload['candidates'][0])
        second.update(title='另一门店', url='https://places.example/branch/2')
        second['restaurant']['address'] = '另一门店具体地址'
        payload['candidates'].append(second)
        origin = dict(payload['origin'], coordinates=dict(latitude=30., longitude=120.,
            system='wgs84', source_url='https://maps.example/origin'))
        nearby = [dict(name=c['title'], address=c['restaurant']['address'],
            coordinates=dict(origin['coordinates'], latitude=30.005),
            route=c['restaurant']['route'], reviews=[dict(c['restaurant']['reviews'][0],
                summary='现做菜品不错，午间曾等了半小时')]) for c in payload['candidates']]
        runtime = SimpleNamespace(create_thread=AsyncMock(), close=AsyncMock(),
            run_turn=AsyncMock(side_effect=[self.location_reply(), SimpleNamespace(
                status='completed', final_message=json.dumps(payload))]))
        with TemporaryDirectory() as directory:
            maps = PlaceMap(directory, provider=Mock())
            maps.discover = Mock(return_value=dict(origin=origin, nearby=nearby))
            worker = ProactiveWorker(TaskStore(Path(directory)/'assistant.db'),
                runtime_factory=lambda: runtime, maps=maps)
            output = asyncio.run(worker.research(dict(timezone='Asia/Shanghai',
                place_policy={}, recent_recommendations=[]), self.trigger, self.context, Event(), self.now))
        self.assertEqual(runtime.run_turn.await_count, 2)  # Location, then one research turn.
        self.assertEqual(len(output['items']), 2)

    def test_enriched_reviews_are_compared_before_publishing(self):
        prompts = []
        location_reply = self.location_reply()
        first, final = deepcopy(self.payload), deepcopy(self.payload)
        first['candidates'][0]['restaurant'].update(route=None, coordinates=None, reviews=[])
        first['candidates'][0]['restaurant']['tradeoff'] = ''
        final['candidates'][0]['restaurant']['tradeoff'] = '晚餐高峰可能排队。'

        class Runtime:
            async def create_thread(self):
                pass

            async def run_turn(self, prompt, **kwargs):
                prompt = json.loads(prompt)
                if prompt['stage'] == 'resolve_location':
                    return location_reply
                prompts.append(prompt)
                return SimpleNamespace(status='completed',
                    final_message=json.dumps(first if len(prompts) == 1 else final))

            async def close(self):
                pass

        origin = dict(ref='event:8:2', name='本次行程地点', address='行程地址',
            coordinates=dict(latitude=30., longitude=120., system='wgs84', source_url='https://maps.example/o'))
        point = dict(name='候选门店', address='门店具体地址',
            coordinates=dict(origin['coordinates'], latitude=30.005),
            route=self.payload['candidates'][0]['restaurant']['route'],
            reviews=[dict(platform='百度地图', source_url='https://reviews.example/new',
                review_date=self.day.isoformat(), summary='晚餐高峰排队')])
        provider = Mock()
        provider.search.return_value = [point]
        provider.reviews.return_value = point['reviews']
        provider.walking_route.return_value = point['route']
        with TemporaryDirectory() as directory:
            maps = PlaceMap(directory, provider=provider)
            maps.discover = Mock(return_value=dict(origin=origin, nearby=[]))
            worker = ProactiveWorker(TaskStore(Path(directory)/'assistant.db'),
                                     runtime_factory=Runtime, maps=maps)
            output = asyncio.run(worker.research(dict(timezone='Asia/Shanghai',
                place_policy={}, recent_recommendations=[]), self.trigger, self.context, Event(), self.now))
        self.assertEqual(len(prompts), 2)
        details = prompts[1]['previous_candidates'][0]['restaurant']
        self.assertEqual(details['reviews'][0]['summary'], '晚餐高峰排队')
        self.assertIn('晚餐高峰可能排队', output['items'][0]['reason'])
        self.assertEqual(output['items'][0]['reason'], self.select(final)['items'][0]['reason'])

    def test_new_origin_with_same_event_ref_replaces_route_and_keeps_fresh_reviews(self):
        payload = deepcopy(self.payload)
        payload['origin'].update(name='变更后的旅馆', address='新路8号')
        old_reviews = [dict(platform='大众点评', source_url=f'https://reviews.example/old/{i}',
            review_date='2020-01-01', summary=f'旧评论{i}') for i in range(4)]
        payload['candidates'][0]['restaurant']['reviews'] = old_reviews
        fresh = dict(platform='百度地图', source_url='https://reviews.example/latest',
                     review_date=self.day.isoformat(), summary='现炒好吃，高峰排队')
        point = dict(name='候选门店', address='门店具体地址',
            coordinates=dict(latitude=30.01, longitude=120., system='wgs84',
                             source_url='https://maps.example/place'),
            reviews=[fresh], route=self.payload['candidates'][0]['restaurant']['route'])
        hotel = dict(name='变更后的旅馆', address='新路8号',
            coordinates=dict(point['coordinates'], latitude=30.015))
        provider = Mock()
        provider.search.return_value = [hotel]
        provider.walking_route.return_value = dict(point['route'], duration_minutes=5)
        maps = PlaceMap('', provider=provider)
        maps.enrich(payload, self.origins, dict(origin=dict(ref='event:8:2',
            name='原旅馆', address='旧路1号', coordinates=point['coordinates']), nearby=[point]))
        self.assertEqual(provider.walking_route.call_args.args[0]['name'], '变更后的旅馆')
        restaurant = payload['candidates'][0]['restaurant']
        self.assertEqual(restaurant['route']['duration_minutes'], 5)
        self.assertEqual(restaurant['reviews'][0], fresh)


if __name__ == '__main__':
    unittest.main()
