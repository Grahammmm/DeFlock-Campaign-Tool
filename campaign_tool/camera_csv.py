"""DF0237-12: the rows of ``data/cameras.csv``, a spreadsheet copy of the community camera map (stdlib only).

Pure functions: nothing here reads or writes a file. ``Site.cameras_csv`` serialises these rows with the shared CSV writer
and the shared formula guard. Every value is text; the property values are the stored ones, never normalised or inferred.
"""
import json

from . import camera_counts
from .content import OSM_NODE_URL, ContentError

COLUMNS = ("osm_url", "latitude", "longitude", "manufacturer", "operator", "direction", "city", "check_status", "checked_on")


def _scalar(value):
    """A stored tag as text: a string verbatim, null/missing blank, a number or boolean as compact JSON."""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _coordinates(feature, index):
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
    # camera_counts._number is overflow-safe: an oversized decoded JSON integer is refused here, not raised as OverflowError
    if not (isinstance(coords, list) and len(coords) >= 2 and all(camera_counts._number(n) for n in coords[:2])):
        raise ContentError(f"map/cameras.geojson.features[{index}].geometry.coordinates: finite coordinates required for CSV")
    return coords[0], coords[1]


def _shapes(boundaries):
    """``[(name, bounding box, outer ring, holes)]`` for every valid named boundary polygon (invalid ones are skipped)."""
    shapes = []
    for feature in (boundaries.get("features") if isinstance(boundaries, dict) else None) or []:
        name = camera_counts._name(feature) if isinstance(feature, dict) else None
        polygons = camera_counts._polygons(feature.get("geometry")) if name is not None else ""
        for rings in [] if isinstance(polygons, str) else polygons:
            xs, ys = [p[0] for p in rings[0]], [p[1] for p in rings[0]]
            shapes.append((name, (min(xs), max(xs), min(ys), max(ys)), camera_counts._Ring(rings[0]),
                           [camera_counts._Ring(hole) for hole in rings[1:]]))
    return shapes


def _city(shapes, x, y):
    """The lexicographically first exact boundary name holding the point (the counts' half-open test), else blank."""
    names = [name for name, (west, east, south, north), outer, holes in shapes
             if west <= x <= east and south <= y <= north and outer.contains(x, y) and not any(h.contains(x, y) for h in holes)]
    return min(names) if names else ""


def camera_rows(cameras, boundaries, checks):
    """One fresh dict with exactly ``COLUMNS`` per camera feature, sorted by raw ``osm_url`` (blank last, stable)."""
    shapes, rows = _shapes(boundaries), []
    for index, feature in enumerate((cameras.get("features") if isinstance(cameras, dict) else None) or []):
        x, y = _coordinates(feature, index)
        props = feature.get("properties")
        props = props if isinstance(props, dict) else {}
        url = props.get("url") if isinstance(props.get("url"), str) else ""
        node = OSM_NODE_URL.fullmatch(url)
        newest = (checks.get(int(node.group(1))) or [None])[0] if node else None  # validated: newest first
        rows.append({"osm_url": url, "latitude": _scalar(y), "longitude": _scalar(x),
                     "manufacturer": _scalar(props.get("manufacturer")), "operator": _scalar(props.get("operator")),
                     "direction": _scalar(props.get("direction")), "city": _city(shapes, x, y),
                     "check_status": newest["status"] if newest else "", "checked_on": newest["checked_on"] if newest else ""})
    return sorted(rows, key=lambda row: (row["osm_url"] == "", row["osm_url"]))
