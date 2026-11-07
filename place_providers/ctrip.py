"""Restaurant reviews from Ctrip's public website, without account cookies.

This is a website fallback, not Ctrip's partner Open API. Search discovers
restaurant pages; page POI IDs and resource IDs are kept separate for comments.
Only normalized restaurant fields and bounded comment samples are cached.
"""

from datetime import datetime
from html import unescape
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .cache import CachedProvider
from .coordinates import gcj02_to_wgs84
from .matching import name_similarity


SEARCH_URL = 'https://m.ctrip.com/restapi/soa2/30668/search'
COMMENT_URL = 'https://m.ctrip.com/restapi/soa2/13444/json/getCommentCollapseList'
SHANGHAI = ZoneInfo('Asia/Shanghai')


def text_content(value):
    return ' '.join(unescape(re.sub(r'<[^>]*>', '', value or '')).split())


def parse_place(html, url):
    script = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not script:
        return None
    state = json.loads(script[1])['props']['pageProps']['initialState']
    raw = state.get('restaurantDetail') or {}
    if not raw.get('poiId') or not raw.get('name') or not raw.get('address'):
        return None
    coordinates = None
    coord = raw.get('gaodeCoord') or {}
    if coord.get('lat') and coord.get('lon'):
        lat, lon = gcj02_to_wgs84(float(coord['lat']), float(coord['lon']))
        coordinates = dict(latitude=lat, longitude=lon, system='wgs84', source_url=url)
    return dict(provider='ctrip_web', uid=re.search(r'/(\d+)\.html$', url)[1],
        poi_id=raw['poiId'], name=text_content(raw['name']), address=text_content(raw['address']),
        url=url, coordinates=coordinates, rating=raw.get('commentScore'),
        price=raw.get('averagePrice'), dishes=(raw.get('foods') or [])[:5])


def parse_reviews(response, place):
    reviews, seen = [], set()
    today = datetime.now(SHANGHAI).date().isoformat()
    for item in (response.get('result') or {}).get('items') or []:
        if str(item.get('resourceId')) != str(place['uid']) or item.get('resourceType') != 12:
            continue
        comment_id = item.get('commentId')
        content = text_content(item.get('content'))
        if not comment_id or comment_id in seen or not content:
            continue
        seen.add(comment_id)
        timestamp = re.fullmatch(r'/Date\((-?\d+)(?:[+-]\d{4})?\)/', item.get('publishTime') or '')
        review_date = ''
        if timestamp:
            try:
                review_date = datetime.fromtimestamp(int(timestamp[1])/1000, SHANGHAI).date().isoformat()
            except (ValueError, OverflowError, OSError):
                continue
        if review_date > today:
            continue
        review = dict(platform='携程', source_url=place['url'],
                      review_date=review_date, summary=content[:240])
        reviews.append((review, float(item.get('score') or 0)))
    reviews.sort(key=lambda pair: pair[0]['review_date'], reverse=True)
    selected = reviews[:3]
    negative = next((pair for pair in reviews if 0 < pair[1] <= 3 and pair not in selected), None)
    if negative:
        selected.append(negative)
    return [review for review, _ in selected]


class CtripReviews(CachedProvider):
    name = 'ctrip_web'

    @staticmethod
    def _fetch(endpoint, params):
        headers = {'Referer': 'https://you.ctrip.com/', 'User-Agent': 'Mozilla/5.0'}
        body = None
        if endpoint == SEARCH_URL:
            body = urlencode(params).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif endpoint == COMMENT_URL:
            body = json.dumps(params).encode()
            headers['Content-Type'] = 'application/json'
        request = Request(endpoint, data=body, headers=headers)
        with urlopen(request, timeout=6) as response:
            return response.read().decode('utf-8') if body is None else json.load(response)

    def search(self, name, address=''):
        def parse(response):
            candidates = []
            for raw in response.get('data') or []:
                url = raw.get('url') or ''
                if raw.get('type') != 'food' or not re.fullmatch(
                        r'https?://you\.ctrip\.com/food/[^/]+/\d+\.html', url):
                    continue
                score = name_similarity(name, text_content(raw.get('word')))
                if score >= .6:
                    candidates.append((score, url.replace('http://', 'https://', 1)))
            candidates.sort(reverse=True)
            return list(dict.fromkeys(url for _, url in candidates))[:3]

        urls = self._query(SEARCH_URL, dict(action='online', source='globalonline',
            keyword=' '.join(filter(None, (name, address)))), parse, ttl=86400, empty=[])
        if not urls and address:
            urls = self._query(SEARCH_URL, dict(action='online', source='globalonline',
                keyword=name), parse, ttl=86400, empty=[])
        return [place for url in urls if (place := self._query(
            url, {}, lambda html, url=url: parse_place(html, url), ttl=86400))]

    def reviews(self, place):
        params = dict(arg=dict(poiId=place['poi_id'], resourceId=int(place['uid']),
            resourceType=12, sortType=1, pageIndex=1, pageSize=10, sourceType=1))
        return self._query(COMMENT_URL, params, lambda response: parse_reviews(response, place), empty=[])
