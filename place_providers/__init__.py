"""External place data adapters; recommendation policy lives outside providers.

Adapters expose search(name, address), nearby(origin, max_km, page),
reviews(place) and walking_route(origin, destination). Places provide a name,
address and WGS84 coordinates, with optional provider-specific identifiers.
Reviews and routes follow the contracts in places. A missing
route is None, never an estimate derived from straight-line distance.
"""
