"""Place research contracts and selection shared by scheduled and preview discovery."""

from datetime import date, datetime
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

from memory.store import recommendation_url


def obj(**properties):
    return dict(type='object', additionalProperties=False, required=list(properties), properties=properties)


TEXT = dict(type='string')
NUMBER = dict(type=['number', 'null'])
COORDINATES = obj(latitude=dict(type='number'), longitude=dict(type='number'),
                  system=dict(type='string', enum=['wgs84', 'gcj02', 'bd09']), source_url=TEXT)
COORDINATES['type'] = ['object', 'null']
ROUTE = obj(distance_km=NUMBER, duration_minutes=NUMBER,
            mode=dict(type='string', enum=['walking', 'cycling', 'driving', 'transit']), source_url=TEXT)
ROUTE['type'] = ['object', 'null']
RESTAURANT = obj(address=TEXT, coordinates=COORDINATES, route=ROUTE,
    reviews=dict(type='array', maxItems=4, items=obj(platform=TEXT, source_url=TEXT,
        review_date=TEXT, summary=TEXT)),
    attraction=dict(type='string', description='直接展示的推荐理由：从实际门店资料提炼具体菜品或体验特色，不只罗列菜名，不用“地道本地菜”等泛称充数。'),
    fit=dict(type='string', description='直接展示：该店如何适合本次行程或已知偏好，不能编造个人口味。'),
    tradeoff=dict(type='string', description='直接展示的实际取舍，如已查到的排队、口味分歧、预约要求；无则空字符串。资料缺失、评论新旧、可否引用、检索过程只能写 summary，严禁写在这里。'),
    recommended_dishes=TEXT, price=TEXT)
RESTAURANT['type'] = ['object', 'null']
LOCATION_ORIGIN = obj(ref=TEXT, name=TEXT, address=TEXT, coordinates=COORDINATES,
                      conditional=dict(type='boolean'))
LOCATION_ORIGIN['type'] = ['object', 'null']
LOCATION_SCHEMA = obj(summary=TEXT, origin=LOCATION_ORIGIN)
RESEARCH_SCHEMA = obj(summary=TEXT,
    origin=obj(ref=TEXT,
        name=dict(type='string', description='用于地图查找的具体场所名称，从行程标题、地点和说明解析；不要复制含“地点待定”的区域描述。'),
        address=dict(type='string', description='具体场所的街道门牌地址，未知留空；场所名称放 name，不能只放在 address。'),
        coordinates=COORDINATES),
    candidates=dict(type='array', maxItems=4, items=obj(
        category=dict(type='string', enum=['restaurant', 'exhibition']),
        title=TEXT, body=TEXT, reason=TEXT, url=TEXT,
        profile_ids=dict(type='array', items=dict(type='integer')),
        dh_fact_ids=dict(type='array', items=TEXT), restaurant=RESTAURANT)))

# Generic discovery defaults, overridable as settings.personalization.place_policy.
DEFAULT_POLICY = dict(review_max_age_days=90, max_route_minutes=30, max_straight_line_km=3)
MODE_LABELS = dict(walking='步行', cycling='骑行', driving='驾车', transit='公共交通')


def origin_options(trigger, context, calendar_timeline=()):
    """Typed location sources, not an ordered list of presumed starting points."""
    result, seen = [], set()
    for event in [trigger['event']] + list(calendar_timeline) if trigger.get('event') else calendar_timeline:
        ref = f"event:{event['id']}:{event['revision']}"
        if ref in seen or event.get('status', 'confirmed') != 'confirmed':
            continue
        seen.add(ref)
        result.append(dict(ref=ref, name=event.get('location', ''),
            title=event.get('title', ''), context=event.get('notes', ''),
            starts_at=event.get('starts_at'), ends_at=event.get('ends_at'),
            scheduled_date=event.get('scheduled_date'), recurrence=event.get('recurrence'),
            field='calendar_event', basis='日程中的地点线索', conditional=True, coordinates=None))
    location = context.get('current_location')
    if location:
        result.append(dict(ref='device_location', name='你提供的大致位置', basis='设备大致定位',
            field='device_location', observed_at=location.get('observed_at'),
            conditional=trigger['kind'] != 'free_time',
            coordinates=dict(latitude=location['latitude'], longitude=location['longitude'],
                             system='wgs84', source_url=''), approximate=True))
    for fact in context.get('historical_locations', []):
        result.append(dict(ref='fact:' + fact['id'], name=fact['value'],
            field=fact.get('field', 'unknown'), context=fact.get('context', ''),
            observed_at=fact.get('observed_at'), temporal_kind=fact.get('temporal_kind'),
            valid_from=fact.get('valid_from'), valid_until=fact.get('valid_until'),
            basis='有用途区分的历史地址', conditional=True, coordinates=None))
    return result


def straight_line_km(a, b):
    """Do not mix map coordinate systems or infer road distance from geometry."""
    if not a or not b or a.get('system') != b.get('system'):
        return None
    if a.get('system') not in ('wgs84', 'gcj02', 'bd09'):
        return None
    for p in (a, b):
        if not (-90 <= p['latitude'] <= 90 and -180 <= p['longitude'] <= 180):
            return None
    lat1, lat2 = radians(a['latitude']), radians(b['latitude'])
    dlat, dlon = lat2 - lat1, radians(b['longitude'] - a['longitude'])
    return 6371.0088 * 2 * asin(min(1, sqrt(sin(dlat / 2)**2 + cos(lat1)*cos(lat2)*sin(dlon / 2)**2)))


def source_links(discovery):
    links = []
    travel = discovery.get('travel', {})
    if travel.get('source_url'):
        links.append(dict(label='交通来源', url=travel['source_url']))
    if travel.get('origin_source_url'):
        links.append(dict(label='出发点位置来源', url=travel['origin_source_url']))
    for review in discovery.get('reviews', []):
        link = dict(label=f"{review['platform']} · {review['review_date']}评价", url=review['source_url'])
        if link not in links:
            links.append(link)
    return links


def select_candidates(research, origins, *, target_date, timezone, now, policy=None):
    """Select specific nearby restaurants and preserve the actual review dates."""
    policy = dict(DEFAULT_POLICY, **(policy or {}))
    today = datetime.fromtimestamp(now, ZoneInfo(timezone)).date()
    expected = next((o for o in origins if o['ref'] == research['origin']['ref']), None)
    chosen, rejected, seen = [], [], set()
    for candidate in research['candidates']:
        item = dict(candidate)
        item.pop('category')
        restaurant = item.pop('restaurant')
        url = recommendation_url(item['url'])
        if not url or url in seen:
            rejected.append(dict(title=item['title'], reason='没有具体详情来源，或同一门店重复'))
            continue
        seen.add(url)
        item['url'] = url
        if not expected:
            rejected.append(dict(title=item['title'], reason='出发点没有对应的日程、定位或历史地点'))
            continue
        origin = dict(research['origin'], basis=expected['basis'], conditional=expected['conditional'])
        if candidate['category'] == 'exhibition':
            if expected['conditional']:
                item['body'] = f"{target_date} 如果在{origin['name']}附近，可作备选。\n" + item['body']
            item['discovery'] = dict(category='exhibition', origin=origin,
                target_date=target_date, researched_at=now)
            chosen.append(item)
            continue
        reason = ''
        if not restaurant or not restaurant['address'] or not restaurant['attraction'] or not restaurant['fit']:
            reason = '缺少具体门店地址、吸引力或与本次需求的关联'
        if reason:
            rejected.append(dict(title=item['title'], reason=reason))
            continue
        reviews = []
        for review in restaurant['reviews']:
            source = recommendation_url(review['source_url'])
            if not source or not review['platform'] or not review['summary']:
                continue
            try:
                reviewed = date.fromisoformat(review['review_date'])
            except ValueError:
                reviewed = None
            if reviewed and reviewed > today:
                continue
            reviews.append(dict(review, source_url=source,
                review_date=reviewed.isoformat() if reviewed else '',
                recent=bool(reviewed and (today-reviewed).days <= policy['review_max_age_days'])))
        travel = None
        route = restaurant['route']
        if route and recommendation_url(route['source_url']):
            minutes = route['duration_minutes']
            distance = route['distance_km']
            if minutes is not None and 0 < minutes <= policy['max_route_minutes']:
                travel = dict(route, kind='route', source_url=recommendation_url(route['source_url']))
                distance_text = f"，路线约 {distance:g} 公里" if distance is not None and distance > 0 else ''
                travel['text'] = f"从{origin['name']}{MODE_LABELS[route['mode']]}约 {minutes:g} 分钟{distance_text}"
            elif minutes is not None and minutes > policy['max_route_minutes']:
                rejected.append(dict(title=item['title'], reason='实际交通时间超过附近推荐范围'))
                continue
        if not travel:
            a, b = expected['coordinates'] or origin['coordinates'], restaurant['coordinates']
            # Device coordinates are supplied by us; researched coordinates must cite their source.
            if (a and (expected['coordinates'] or recommendation_url(a['source_url']))
                    and b and recommendation_url(b['source_url'])):
                distance = straight_line_km(a, b)
                if distance is not None and distance <= policy['max_straight_line_km']:
                    travel = dict(kind='straight_line', distance_km=round(distance, 1),
                        duration_minutes=None, mode=None, source_url=recommendation_url(b['source_url']),
                        origin_source_url=recommendation_url(a['source_url']),
                        text=f"距{origin['name']}直线约 {distance:.1f} 公里")
            reason = '无法确定到店距离，或不在附近范围'
        if not travel:
            rejected.append(dict(title=item['title'], reason=reason))
            continue
        if expected.get('approximate'):
            travel['text'] += '（定位精度约 1 公里）'
        if expected['conditional']:
            location_note = f"{target_date} 如果仍在{origin['name']}附近，可作备选。"
        else:
            location_note = f"{target_date} 的用餐备选。"
        body = [restaurant['address'], location_note, travel['text']]
        if restaurant['recommended_dishes']:
            body.append('可考虑：' + restaurant['recommended_dishes'])
        if restaurant['price']:
            body.append('价格参考：' + restaurant['price'])
        item['body'] = '\n'.join(body)
        item['reason'] = ' '.join(filter(None, (restaurant['attraction'], restaurant['fit'], restaurant['tradeoff'])))
        item['discovery'] = dict(category='restaurant', origin=origin, name=item['title'],
            address=restaurant['address'], coordinates=restaurant['coordinates'], travel=travel,
            reviews=reviews, attraction=restaurant['attraction'], fit=restaurant['fit'],
            tradeoff=restaurant['tradeoff'], target_date=target_date, researched_at=now)
        item['discovery']['sources'] = source_links(item['discovery'])
        chosen.append(item)
    return dict(items=chosen, rejected=rejected)
