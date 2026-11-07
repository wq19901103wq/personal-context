"""Automatic branch matching and dated public review retrieval."""

from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from personal_assistant.integrations import Integrations
from personal_assistant.maps import PlaceMap
from personal_assistant.place_providers.coordinates import wgs84_to_gcj02
from personal_assistant.place_providers.ctrip import CtripReviews, SEARCH_URL, COMMENT_URL, SHANGHAI


class CtripReviewsTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.today = datetime.now(SHANGHAI)
        self.point = dict(provider='maps', uid='map-east', name='菜馆(东店)', address='东路8号',
                          coordinates=dict(latitude=30., longitude=120., system='wgs84'))
        self.origin = dict(ref='device', name='当前位置',
                           coordinates=dict(latitude=30.001, longitude=120., system='wgs84'))

    @staticmethod
    def page(name, address, poi_id, latitude=30.):
        lat, lon = wgs84_to_gcj02(latitude, 120.)
        detail = dict(poiId=poi_id, name=name, address=address,
                      gaodeCoord=dict(lat=lat, lon=lon), commentScore=4.5,
                      foods=['现炒菜'], averagePrice=60)
        state = dict(restaurantDetail=detail, user=dict(nickname='private-profile'))
        return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
            dict(props=dict(pageProps=dict(initialState=state)))) + '</script>'

    def review(self, identity, days, content, **fields):
        timestamp = round((self.today-timedelta(days=days)).timestamp()*1000)
        return dict(commentId=identity, resourceId=204, resourceType=12,
                    publishTime=f'/Date({timestamp}+0800)/', content=content,
                    user=dict(nickname='private-reviewer'), **fields)

    def test_search_matches_branch_address_and_coordinates_then_reads_latest_reviews(self):
        def fetch(endpoint, params):
            if endpoint == SEARCH_URL:
                self.assertEqual(params['keyword'], '菜馆(东店) 东路8号')
                return dict(data=[dict(type='hotel', word='菜馆(东店)', url='https://you.ctrip.com/food/1/999.html'),
                    dict(type='food', word='菜馆(西店)', url='http://you.ctrip.com/food/1/205.html'),
                    dict(type='food', word='菜馆(东店)', url='http://you.ctrip.com/food/1/206.html'),
                    dict(type='food', word='菜馆(东店)', url='http://you.ctrip.com/food/1/204.html'),
                    dict(type='food', word='另一家餐厅', url='http://you.ctrip.com/food/1/207.html')])
            if endpoint == COMMENT_URL:
                self.assertEqual(params['arg'], dict(poiId=904, resourceId=204, resourceType=12,
                    sortType=1, pageIndex=1, pageSize=10, sourceType=1))
                latest = self.review(1, 1, '<p>现炒&amp;分量足</p>', score=5.)
                wrong = dict(self.review(2, 0, '其他分店', score=5.), resourceId=205)
                return dict(result=dict(items=[latest, latest, wrong,
                    self.review(3, -1, '未来评论', score=5.),
                    self.review(4, 2, '口味好', score=5.),
                    self.review(5, 3, '出餐快', score=4.),
                    self.review(6, 100, '高峰排队较久', score=2.)]))
            if endpoint.endswith('/205.html'):
                return self.page('菜馆(西店)', '西路1号', 905)
            if endpoint.endswith('/206.html'):
                return self.page('菜馆(东店)', '东路8号', 906, latitude=31.)
            return self.page('菜馆(东店)', '东路8号', 904)

        transport = Mock(side_effect=fetch)
        ctrip = CtripReviews(self.root/'cache', fetch=transport)
        places = SimpleNamespace(name='maps', nearby=Mock(return_value=[self.point]))
        baidu = SimpleNamespace(name='other', search=Mock(return_value=[dict(self.point, provider='other')]),
            reviews=Mock(return_value=[dict(platform='地图评价', review_date='2020-01-01',
                summary='旧评论', source_url='https://maps.example/reviews')]))
        route = dict(distance_km=.2, duration_minutes=3, mode='walking', source_url='https://maps.example/route')
        routes = SimpleNamespace(name='maps', walking_route=Mock(return_value=route))
        maps = PlaceMap(self.root/'cache', providers=dict(places=places, routes=routes, reviews=[ctrip, baidu]))
        point = maps.discover([self.origin])['nearby'][0]
        self.assertEqual(point['route'], route)
        self.assertEqual(len(point['reviews']), 4)
        self.assertEqual(point['reviews'][0]['summary'], '现炒&分量足')
        self.assertEqual(point['reviews'][0]['review_date'], (self.today-timedelta(days=1)).date().isoformat())
        self.assertEqual(point['reviews'][-1]['review_date'], (self.today-timedelta(days=100)).date().isoformat())
        self.assertTrue(all(r['platform'] == '携程' for r in point['reviews']))
        self.assertNotIn('旧评论', str(point['reviews']))
        maps.discover([self.origin])
        self.assertEqual(transport.call_count, 5)  # Search, three details, comments, all reused.
        for path in (self.root/'cache').glob('*.json'):
            cached = path.read_text()
            self.assertNotIn('private-profile', cached)
            self.assertNotIn('private-reviewer', cached)
            self.assertNotIn('__NEXT_DATA__', cached)

    def test_failed_review_source_preserves_other_reviews_and_route(self):
        ctrip = CtripReviews(self.root/'cache', fetch=Mock(side_effect=OSError('unavailable')))
        review = dict(platform='地图评价', source_url='https://maps.example/reviews',
                      review_date=self.today.date().isoformat(), summary='现炒好吃')
        source = SimpleNamespace(name='maps', reviews=Mock(return_value=[review]),
                                 walking_route=Mock(return_value=dict(duration_minutes=3)))
        maps = PlaceMap(self.root/'cache', providers=dict(places=source, routes=source, reviews=[ctrip, source]))
        detail = maps._details(self.point, self.origin)
        self.assertEqual(detail['reviews'], [review])
        self.assertEqual(detail['route']['duration_minutes'], 3)

    def test_review_lists_single_sources_and_disabled_are_configurable(self):
        config = self.root/'integrations.json'
        for selection in (['ctrip_web', 'baidu_web'], 'baidu_web', 'none', []):
            config.write_text(json.dumps(dict(place_providers=dict(reviews=selection))))
            reviews = Integrations(self.root).place_providers(self.root/'cache')['reviews']
            names = [p.name for p in reviews] if isinstance(reviews, list) else reviews.name if reviews else 'none'
            self.assertEqual(names, selection)


if __name__ == '__main__':
    unittest.main()
