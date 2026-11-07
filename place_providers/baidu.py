"""Baidu public map adapter: normalized places, user reviews and walking routes."""

import base64
import json
from datetime import date
from math import atan2, cos, pi, sin, sqrt
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


from .cache import CachedProvider
from .coordinates import gcj_offset
MAP_URL = 'https://map.baidu.com/'
COMMENT_URL = 'https://ugcapi.baidu.com/richindex/2/comment'
MC_BANDS = [12890594.86, 8362377.87, 5591021, 3481989.83, 1678043.12, 0]
# Baidu's public web map projection: BD09 Mercator -> BD09 longitude/latitude.
MC_TO_LL = [
    [1.410526172116255e-8, 8.98305509648872e-6, -1.9939833816331, 200.9824383106796,
     -187.2403703815547, 91.6087516669843, -23.38765649603339, 2.57121317296198,
     -.03801003308653, 17337981.2],
    [-7.435856389565537e-9, 8.983055097726239e-6, -.78625201886289, 96.32687599759846,
     -1.85204757529826, -59.36935905485877, 47.40033549296737, -16.50741931063887,
     2.28786674699375, 10260144.86],
    [-3.030883460898826e-8, 8.98305509983578e-6, .30071316287616, 59.74293618442277,
     7.357984074871, -25.38371002664745, 13.45380521110908, -3.29883767235584,
     .32710905363475, 6856817.37],
    [-1.981981304930552e-8, 8.983055099779535e-6, .03278182852591, 40.31678527705744,
     .65659298677277, -4.44255534477492, .85341911805263, .12923347998204,
     -.04625736007561, 4482777.06],
    [3.09191371068437e-9, 8.983055096812155e-6, .00006995724062, 23.10934304144901,
     -.00023663490511, -.6321817810242, -.00663494467273, .03430082397953,
     -.00466043876332, 2555164.4],
    [2.890871144776878e-9, 8.983055095805407e-6, -3.068298e-8, 7.47137025468032,
     -.00000353937994, -.02145144861037, -.00001234426596, .00010322952773,
     -.00000323890364, 826088.5],
]


def mercator_to_bd09(x, y):
    coefficients = next(c for band, c in zip(MC_BANDS, MC_TO_LL) if abs(y) >= band)
    t = abs(y) / coefficients[9]
    lon = coefficients[0] + coefficients[1] * abs(x)
    lat = sum(coefficients[i + 2] * t**i for i in range(7))
    return (lat if y >= 0 else -lat), (lon if x >= 0 else -lon)


def bd09_to_mercator(lat, lon):
    # Invert the same projection rather than approximate with Web Mercator.
    low, high = 0., MC_BANDS[0]
    for _ in range(45):
        mid = (low + high) / 2
        if mercator_to_bd09(0, mid)[0] < abs(lat):
            low = mid
        else:
            high = mid
    y = (low + high) / 2
    c = next(c for band, c in zip(MC_BANDS, MC_TO_LL) if y >= band)
    x = (abs(lon) - c[0]) / c[1]
    return (x if lon >= 0 else -x), (y if lat >= 0 else -y)


def bd09_to_wgs84(lat, lon):
    x, y = lon - .0065, lat - .006
    z = sqrt(x*x+y*y) - .00002*sin(y*pi*3000/180)
    theta = atan2(y, x) - .000003*cos(x*pi*3000/180)
    gcj_lat, gcj_lon = z*sin(theta), z*cos(theta)
    lat, lon = gcj_lat, gcj_lon
    for _ in range(3):
        dy, dx = gcj_offset(lat, lon)
        lat, lon = gcj_lat-dy, gcj_lon-dx
    return lat, lon


def wgs84_to_bd09(lat, lon):
    dy, dx = gcj_offset(lat, lon)
    y, x = lat+dy, lon+dx
    z = sqrt(x*x+y*y) + .00002*sin(y*pi*3000/180)
    theta = atan2(y, x) + .000003*cos(x*pi*3000/180)
    return z*sin(theta)+.006, z*cos(theta)+.0065


def decoded(value):
    # The comment service returns a second layer of escaped Unicode and slashes.
    return json.loads(json.dumps(value or '', ensure_ascii=False).replace(r'\\u', r'\u')).replace(r'\/', '/')


def parse_reviews(raw, uid, *, encoded=False):
    reviews = []
    for item in raw:
        if 'aigc' in ' '.join(str(item.get(k) or '') for k in ('src', 'from_source', 'from')).lower():
            continue
        if item.get('poi_uid') and item['poi_uid'] != uid:
            continue
        content = decoded(item.get('content'))
        if encoded:
            try:
                content = base64.b64decode(content, validate=True).decode('utf-8')
            except (ValueError, UnicodeError):
                pass
        if not content.strip():
            continue
        review_date = (item.get('date') or '')[:10]
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', review_date) and review_date > date.today().isoformat():
            continue
        source = next((decoded(item.get(k)) for k in ('one_url', 'comment_url', 'comment_jump_url')
                       if decoded(item.get(k)).startswith(('https://', 'http://'))), '')
        source = source or 'https://ugc.map.baidu.com/cube/comment/choicepage?' + urlencode(dict(uid=uid, tab=1))
        platform = decoded(item.get('cn_name')) or {
            'baidumap': '百度地图', 'dianping': '大众点评', 'ctrip': '携程',
        }.get(item.get('src'), '地图收录评价')
        reviews.append(dict(platform=platform, source_url=source, review_date=review_date,
                            summary=content[:240]))
    return sorted(reviews, key=lambda r: r['review_date'], reverse=True)


def parse_place(raw):
    geo = re.search(r'\|(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?);', raw.get('geo', ''))
    if not geo or not raw.get('uid') or not raw.get('name'):
        return None
    x, y = map(float, geo.groups())
    lat, lon = bd09_to_wgs84(*mercator_to_bd09(x, y))
    url = MAP_URL + '?' + urlencode(dict(qt='inf', uid=raw['uid']))
    ext = raw.get('ext') or {}
    if isinstance(ext, str):
        ext = json.loads(ext)
    detail = ext.get('detail_info', {})
    menus = (detail.get('gd_rcmd_dish') or {}).get('featured_menus', [])
    return dict(provider='baidu_web', uid=raw['uid'], name=raw['name'], aliases=raw.get('alias') or [],
        address=raw.get('addr', ''), city_id=raw.get('city_id', 1), city=raw.get('city_name', ''),
        url=url, mercator=[x, y],
        coordinates=dict(latitude=lat, longitude=lon, system='wgs84', source_url=url),
        category=detail.get('tag', ''), rating=detail.get('overall_rating', ''),
        price=detail.get('price', ''), hours=detail.get('shop_hours', ''),
        dishes=[m['name'] for m in menus[:5] if m.get('name')],
        reviews=parse_reviews(detail.get('comments') or [], raw['uid'], encoded=True)[:4])


class BaiduMaps(CachedProvider):
    # Public website fallback, separate from Baidu's official API/MCP.
    name = 'baidu_web'

    @staticmethod
    def _fetch(endpoint, params):
        request = Request(endpoint + '?' + urlencode(params), headers={
            'Referer': MAP_URL,
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'})
        with urlopen(request, timeout=6) as response:
            return json.load(response)

    def places(self, **params):
        def parse(response):
            raw = response.get('content', [])
            places = [p for item in raw if (p := parse_place(item))] if isinstance(raw, list) else []
            for place in places:
                if place['city_id'] == 1:
                    place['city_id'] = response.get('current_city', {}).get('code', 1)
            return places
        return self._query(MAP_URL, dict(ie='utf-8', **params), parse, ttl=86400, empty=[])

    def nearby(self, origin, *, max_km, page=0):
        coordinates = origin['coordinates']
        x, y = origin.get('mercator') or bd09_to_mercator(*wgs84_to_bd09(
            coordinates['latitude'], coordinates['longitude']))
        radius = max_km*1000 / max(.2, cos(origin['coordinates']['latitude']*pi/180))
        bounds = f'({x-radius},{y-radius};{x+radius},{y+radius})'
        return self.places(qt='nb', wd='美食', c=origin.get('city_id', 1), nb_x=x, nb_y=y,
            ar=bounds, b=bounds, r=radius, gr_radius=radius, pn=page, l=15)

    def search(self, name, address=''):
        return self.places(qt='s', wd=' '.join(filter(None, (name, address))), c=1)

    def reviews(self, place):
        def query(tab):
            params = dict(uid=place['uid'], poi_name=place['name'], std_tag=place['category'],
                          pageIndex=1, pageCount=10, pic_videos=1, tab=tab)
            return self._query(COMMENT_URL, params,
                lambda response: parse_reviews((response.get('data') or {}).get('comment_list') or [], place['uid']),
                empty=[])
        # The live API identifies tab 5 as newest and tab 3 as negative reviews.
        latest, negative = query(5), query(3)
        result = []
        for review in [*latest[:3], *negative[:1], *place.get('reviews', [])]:
            if review not in result:
                result.append(review)
        return result[:4]

    def walking_route(self, origin, destination):
        def point(place):
            coordinates = place.get('coordinates')
            if not coordinates or coordinates['system'] != 'wgs84':
                return None
            x, y = place.get('mercator') or bd09_to_mercator(*wgs84_to_bd09(
                coordinates['latitude'], coordinates['longitude']))
            return f"1$$$${x:.2f},{y:.2f}$${place['name']}$$$$$$"
        start, end = point(origin), point(destination)
        if start is None or end is None:
            return None
        city = destination.get('city_id', 1)
        params = dict(qt='walk', sn=start, en=end, sc=city, ec=city, c=city,
                      version=6, run=0, spath_type=1, ie='utf-8')

        def parse(response):
            for route in (response.get('content') or {}).get('routes', []):
                legs = route.get('legs') or []
                distance = sum(leg.get('distance', 0) for leg in legs)
                duration = sum(leg.get('duration', 0) for leg in legs)
                if distance > 0 and duration > 0:
                    return dict(distance_km=round(distance/1000, 2),
                        duration_minutes=max(1, round(duration/60)), mode='walking',
                        source_url=MAP_URL + '?' + urlencode(params))
            return None
        return self._query(MAP_URL, params, parse)
