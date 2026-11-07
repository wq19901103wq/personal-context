"""Resolve places and enrich recommendations through a replaceable map adapter."""

from concurrent.futures import ThreadPoolExecutor

from integrations import Integrations
from place_providers.matching import match_place
from places import straight_line_km


class PlaceMap:
    def __init__(self, cache_dir, *, provider=None, providers=None):
        if provider is not None:
            providers = dict(places=provider, routes=provider, reviews=provider)
        if providers is None:
            from pathlib import Path
            providers = Integrations(Path(cache_dir).parent).place_providers(cache_dir)
        self.provider = providers['places']
        self.routes = providers['routes']
        reviews = providers['reviews']
        self.reviews = reviews if isinstance(reviews, list) else [reviews] if reviews else []
        self._origin_points = {}

    def resolve(self, name, address=''):
        places = self.provider.search(name, address)
        return match_place(name, address, places)

    def _origin(self, origin, *, address=''):
        address = address or origin.get('address', '')
        coordinates = origin.get('coordinates')
        if coordinates and coordinates['system'] == 'wgs84':
            point = self._origin_points.get(origin['ref'], {})
            if point.get('coordinates') != coordinates:
                point = {}
            return dict(point, name=origin['name'], address=address, coordinates=coordinates)
        return self.resolve(origin['name'], address)

    def _details(self, point, origin):
        reviews = []
        for provider in self.reviews:
            review_point = self._on_provider(provider, point)
            if review_point:
                for review in provider.reviews(review_point):
                    if review not in reviews:
                        reviews.append(review)
        reviews.sort(key=lambda r: (r.get('review_date', ''),
                                   r.get('platform') == '大众点评'), reverse=True)
        return dict(point, reviews=reviews[:4], route=self._route(origin, point))

    def _on_provider(self, provider, point):
        if provider is None:
            return None
        if provider is self.provider or point.get('provider') == provider.name:
            return point
        # The same chain can have several nearby branches. Match name/address again,
        # never query another platform with the first platform's POI ID.
        candidates = provider.search(point['name'], point['address'])
        candidates = [p for p in candidates if (
            (distance := straight_line_km(point.get('coordinates'), p.get('coordinates'))) is None
            or distance <= .5)]
        return match_place(point['name'], point['address'], candidates)

    def _route(self, origin, point):
        destination = self._on_provider(self.routes, point) if origin else None
        return self.routes.walking_route(origin, destination) if destination else None

    def _public_origin(self, origin, point):
        # Keep provider-specific lookup metadata out of model-facing origins.
        self._origin_points[origin['ref']] = point
        if len(self._origin_points) > 16:
            self._origin_points.pop(next(iter(self._origin_points)))
        return dict(ref=origin['ref'], name=point['name'], address=point['address'],
                    coordinates=point['coordinates'])

    def discover(self, origins, *, max_km=3, page=0):
        for origin in origins[:2]:
            point = self._origin(origin)
            if not point:
                continue
            nearby = [dict(p, distance_km=round(distance, 2))
                for p in self.provider.nearby(point, max_km=max_km, page=page)
                if (distance := straight_line_km(point['coordinates'], p['coordinates'])) is not None
                and distance <= max_km][:8]
            # Fetch bounded detail samples before selection, so the model can compare
            # real travel costs and user reviews instead of relying on a star rating.
            with ThreadPoolExecutor(max_workers=4) as pool:
                nearby = list(pool.map(lambda p: self._details(p, point), nearby))
            return dict(origin=self._public_origin(origin, point), nearby=nearby)
        return dict(origin=None, nearby=[])

    def enrich(self, research, origins, discovered):
        """Reuse retrieved details, keeping each route tied to its actual origin."""
        expected = next((o for o in origins if o['ref'] == research['origin']['ref']), None)
        if not expected:
            return
        origin = discovered.get('origin')
        requested = research['origin']
        same_origin = bool(origin and origin['ref'] == expected['ref'] and (
            expected.get('coordinates') or
            match_place(requested['name'], requested['address'], [origin])))
        if not same_origin:
            # The model resolves venue names from the event's title/location/notes.
            # Use that name for lookup; device coordinates remain supplied by us.
            name = expected['name'] if expected.get('coordinates') else requested['name'] or expected['name']
            point = self._origin(dict(expected, name=name),
                                 address=requested['address'])
            origin = self._public_origin(expected, point) if point else None
        if origin:
            research['origin'] = origin

        def enrich_candidate(candidate):
            restaurant = candidate['restaurant']
            if candidate['category'] != 'restaurant' or not restaurant:
                return
            point = match_place(candidate['title'], restaurant['address'], discovered['nearby'])
            if point:
                if not same_origin:
                    # A discovered route cannot be reused for another starting point.
                    restaurant['route'] = None
                    point = dict(point, route=self._route(origin, point))
            else:
                point = self.resolve(candidate['title'], restaurant['address'])
                if point:
                    point = self._details(point, origin)
            if not point:
                return
            restaurant.update(address=point['address'], coordinates=point['coordinates'])
            if point['route']:
                restaurant['route'] = point['route']
            reviews = []
            for review in [*point['reviews'], *restaurant['reviews']]:
                if review not in reviews:
                    reviews.append(review)
            # Dedicated review tools can add Dianping samples independently of maps.
            reviews.sort(key=lambda r: (r.get('review_date', ''),
                                       r.get('platform') == '大众点评'), reverse=True)
            restaurant['reviews'] = reviews[:4]

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(enrich_candidate, research['candidates'][:4]))
        return origin
