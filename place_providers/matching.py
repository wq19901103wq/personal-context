"""Match a named branch across independent place providers."""

from difflib import SequenceMatcher
import re


def normalized(value):
    return re.sub(r'[^\w]', '', value).lower()


def name_similarity(a, b):
    a, b = normalized(a), normalized(b)
    if not a or not b:
        return 0.
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b))
    aa, bb = {a[i:i+2] for i in range(len(a)-1)}, {b[i:i+2] for i in range(len(b)-1)}
    return max(SequenceMatcher(None, a, b).ratio(), 2*len(aa & bb)/max(1, len(aa)+len(bb)))


def match_place(name, address, places):
    """Do not silently pick the first search hit or another branch of a chain."""
    ranked = []
    for place in places:
        aliases = [place['name'], *place.get('aliases', [])]
        score = max(name_similarity(name, alias) for alias in aliases)
        if address:
            numbers = re.findall(r'\d+(?:-\d+)?号', address)
            other = re.findall(r'\d+(?:-\d+)?号', place['address'])
            if numbers and other and not set(numbers) & set(other):
                continue
            score += .2 * name_similarity(address, place['address'])
        if score >= .6:
            ranked.append((score, place))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0]-ranked[1][0] < .08):
        return None
    return ranked[0][1]
