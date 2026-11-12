"""Count community-mapped camera points inside city boundary polygons (stdlib only).

Coordinates are GeoJSON ``[lon, lat]``; problems name a feature index and reason, never coordinates or values.
"""
import math
import sys
from fractions import Fraction

from .agency_cards import text as plain_text

_MIN_NORMAL = sys.float_info.min  # below this a crossing product may have lost precision


def _number(n):
    """A finite int or float usable by this geometry engine; reject non-representable integers."""
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return False
    try:
        return math.isfinite(n)
    except OverflowError:
        return False


def valid_ring(ring):
    """A closed linear ring: at least 4 finite numeric [x, y, ...] positions, first and last equal (never auto-closed)."""
    return isinstance(ring, list) and len(ring) >= 4 and all(
        isinstance(p, list) and len(p) >= 2 and _number(p[0]) and _number(p[1]) for p in ring) and ring[0] == ring[-1]


def _edges(ring):
    """Non-horizontal edges as (x1, y1, x2, y2); a horizontal edge never crosses the ray."""
    return [(a[0], a[1], b[0], b[1]) for a, b in zip(ring, ring[1:] + ring[:1]) if a[1] != b[1]]


def _crosses(x, y, edges):
    """Even-odd ray cast, half-open on edges: a point on a shared edge is inside exactly one neighbour."""
    inside = False
    for x1, y1, x2, y2 in edges:
        if (y1 > y) != (y2 > y) and _left_of_crossing(x, y, x1, y1, x2, y2):
            inside = not inside
    return inside


def _left_of_crossing(x, y, x1, y1, x2, y2):
    """``x < x1 + (y - y1) * (x2 - x1) / (y2 - y1)``; floats while every step is finite, exact otherwise."""
    dy, dx, dn = y - y1, x2 - x1, y2 - y1  # finite inputs can still overflow here (or exceed float range as ints)
    if not (_number(dy) and _number(dx) and _number(dn) and dn):  # dn == 0: mixed int/float operands rounded together
        return _exact_left_of_crossing(x, y, x1, y1, x2, y2)
    product = dy * dx
    if not _number(product) or (dy and dx and abs(product) < _MIN_NORMAL):  # overflowed or underflowed product
        return _exact_left_of_crossing(x, y, x1, y1, x2, y2)
    try:
        crossing = x1 + product / dn
    except OverflowError:  # integer quotient too large for a float
        return _exact_left_of_crossing(x, y, x1, y1, x2, y2)
    if not _number(crossing):  # never compare against infinity or NaN
        return _exact_left_of_crossing(x, y, x1, y1, x2, y2)
    return x < crossing


def _exact_left_of_crossing(x, y, x1, y1, x2, y2):
    """Exact ``x < x1 + (y - y1) * (x2 - x1) / (y2 - y1)`` from the original numbers (y2 != y1)."""
    return Fraction(x) < Fraction(x1) + (Fraction(y) - Fraction(y1)) * (Fraction(x2) - Fraction(x1)) / (
        Fraction(y2) - Fraction(y1))


def point_in_ring(x, y, ring):
    return valid_ring(ring) and _crosses(x, y, _edges(ring))


def point_in_polygon(pt, rings):
    """Inside ring 0 and inside none of the holes (rings 1..n); false if any supplied ring is invalid."""
    return isinstance(rings, list) and bool(rings) and all(valid_ring(r) for r in rings) and point_in_ring(
        pt[0], pt[1], rings[0]) and not any(point_in_ring(pt[0], pt[1], hole) for hole in rings[1:])


class _Ring:
    """Edges bucketed into horizontal bands, so a point tests only the edges that span its y."""
    def __init__(self, ring):
        edges, ys = _edges(ring), [p[1] for p in ring]
        self.low, self.high = min(ys), max(ys)
        span = self.high - self.low  # finite inputs can still overflow (even as an int too big for a float): one band then
        self.count = max(1, int(len(edges) ** 0.5)) if _number(span) else 1
        self.step = (span / self.count or 1.0) if _number(span) else 1.0
        self.bands = [[] for _ in range(self.count)]
        for edge in edges:
            for band in range(self._band(min(edge[1], edge[3])), self._band(max(edge[1], edge[3])) + 1):
                self.bands[band].append(edge)

    def _band(self, y):
        if self.count == 1:
            return 0
        # Select endpoint bands before division: a tiny span can overflow a distant query's quotient.
        if y <= self.low:
            return 0
        if y >= self.high:
            return self.count - 1
        return min(self.count - 1, max(0, int((y - self.low) / self.step)))

    def contains(self, x, y):
        return _crosses(x, y, self.bands[self._band(y)])


def _name(feature):
    properties = feature.get("properties")
    name = (properties.get("BASENAME") or properties.get("NAME")) if isinstance(properties, dict) else None
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or not plain_text(name):
        return None
    return name  # exact, as the browser matches BASENAME || NAME


def _polygons(geometry):
    """List of polygons (each a list of rings), or a reason string."""
    kind = geometry.get("type") if isinstance(geometry, dict) else None
    if kind not in ("Polygon", "MultiPolygon"):
        return "unsupported geometry type (Polygon or MultiPolygon only)"
    polygons = [geometry.get("coordinates")] if kind == "Polygon" else geometry.get("coordinates")
    if not isinstance(polygons, list) or not polygons or any(
            not isinstance(rings, list) or not rings or not all(valid_ring(r) for r in rings) for rings in polygons):
        return "invalid ring (needs a closed ring of 4 or more finite numeric positions)"
    return polygons


def _points(cameras_doc):
    features = cameras_doc.get("features") if isinstance(cameras_doc, dict) else None
    coords = [f.get("geometry", {}).get("coordinates") if isinstance(f, dict) and isinstance(f.get("geometry"), dict)
              else None for f in (features if isinstance(features, list) else [])]
    return [(c[0], c[1]) for c in coords if isinstance(c, list) and len(c) >= 2 and _number(c[0]) and _number(c[1])]


def count_by_name(cameras_doc, boundaries_doc):
    """``({name: points inside}, problems)``; a point counts once per name even if features overlap."""
    points = _points(cameras_doc)
    features = boundaries_doc.get("features") if isinstance(boundaries_doc, dict) else None
    if not isinstance(features, list):
        return {}, ["city boundary features: expected a list"]
    inside, problems = {}, []
    for index, feature in enumerate(features):
        name = _name(feature) if isinstance(feature, dict) else None
        polygons = _polygons(feature.get("geometry")) if isinstance(feature, dict) else "expected an object"
        if isinstance(polygons, str) or name is None:
            reason = polygons if isinstance(polygons, str) else "no BASENAME or NAME plain text (120 characters or fewer)"
            problems.append(f"city boundary feature {index}: {reason}")
            continue
        found = inside.setdefault(name, set())
        for rings in polygons:
            xs, ys = [p[0] for p in rings[0]], [p[1] for p in rings[0]]
            west, east, south, north = min(xs), max(xs), min(ys), max(ys)
            outer, holes = _Ring(rings[0]), [_Ring(hole) for hole in rings[1:]]
            for number, (x, y) in enumerate(points):
                if number not in found and west <= x <= east and south <= y <= north \
                        and outer.contains(x, y) and not any(hole.contains(x, y) for hole in holes):
                    found.add(number)
    return {name: len(found) for name, found in inside.items()}, problems
