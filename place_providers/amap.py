"""Amap official Web Service API for POIs and actual walking routes."""

import json
from urllib.parse import urlencode
from urllib.request import urlopen

from .cache import CachedProvider
from .coordinates import gcj02_to_wgs84, wgs84_to_gcj02


API = 'https://restapi.amap.com/v5/'


def location(point):
    coordinates = point['coordinates']
    lat, lon = coordinates['latitude'], coordinates['longitude']
    if coordinates['system'] == 'wgs84':
        lat, lon = wgs84_to_gcj02(lat, lon)
    elif coordinates['system'] != 'gcj02':
        raise ValueError('高德路线需要 WGS84 或 GCJ02 坐标')
    return f'{lon:.6f},{lat:.6f}'


def parse_place(raw):
    if not raw.get('id') or not raw.get('location') or not raw.get('name'):
        return None
    lon, lat = map(float, raw['location'].split(','))
    lat, lon = gcj02_to_wgs84(lat, lon)
    url = 'https://www.amap.com/place/' + raw['id']
    business = raw.get('business') or {}
    return dict(provider='amap', uid=raw['id'], name=raw['name'], aliases=[],
        address=''.join(raw.get(k) or '' for k in ('pname', 'cityname', 'adname', 'address')),
        city=raw.get('cityname') or '', url=url,
        coordinates=dict(latitude=lat, longitude=lon, system='wgs84', source_url=url),
        category=raw.get('type') or '', rating=business.get('rating') or '',
        price=business.get('cost') or '', hours=business.get('opentime_today') or '',
        dishes=[], reviews=[])


class Amap(CachedProvider):
    name = 'amap'

    def __init__(self, cache_dir, api_key, *, fetch=None):
        super().__init__(cache_dir, fetch=fetch)
        self.api_key = api_key

    @staticmethod
    def _fetch(endpoint, params):
        with urlopen(endpoint + '?' + urlencode(params), timeout=6) as response:
            return json.load(response)

    def _api(self, path, params, parse, **kwargs):
        def checked(response):
            if str(response.get('status')) != '1':
                raise ValueError('高德 API 请求未成功')
            return parse(response)
        return self._query(API + path, dict(key=self.api_key, **params), checked, **kwargs)

    def _places(self, path, **params):
        return self._api(path, params,
            lambda response: [p for raw in response.get('pois', []) if (p := parse_place(raw))],
            ttl=86400, empty=[])

    def search(self, name, address=''):
        return self._places('place/text', keywords=' '.join(filter(None, (name, address)))[:80],
                            show_fields='business', page_size=10, page_num=1)

    def nearby(self, origin, *, max_km, page=0):
        return self._places('place/around', location=location(origin), radius=round(max_km*1000),
                            types='050000', sortrule='distance', show_fields='business',
                            page_size=8, page_num=page+1)

    def walking_route(self, origin, destination):
        start, end = location(origin), location(destination)
        # Public navigation URL must never include the API credential.
        url = 'https://uri.amap.com/navigation?' + urlencode(dict(
            **{'from': start, 'to': end}, mode='walk', coordinate='gaode', callnative=0))

        def parse(response):
            for path in (response.get('route') or {}).get('paths', []):
                distance = float(path.get('distance') or 0)
                duration = float((path.get('cost') or {}).get('duration') or 0)
                if distance > 0 and duration > 0:
                    return dict(distance_km=round(distance/1000, 2),
                        duration_minutes=max(1, round(duration/60)), mode='walking', source_url=url)
            return None
        return self._api('direction/walking', dict(origin=start, destination=end,
                                                  show_fields='cost'), parse)
