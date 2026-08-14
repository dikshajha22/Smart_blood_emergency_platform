"""Geospatial helpers implemented in pure Python.

The project targets plain SQLite (no PostGIS / SpatiaLite), so proximity search is
done in two stages:

1. A cheap **bounding-box** filter that the database can answer with an index on
   ``(latitude, longitude)``. This throws away the vast majority of rows.
2. An exact **haversine** great-circle distance computed in Python on the small
   surviving set, used for the true radius test and for ranking.

That keeps queries fast without adding a geo extension or new dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

#: Mean earth radius in kilometres (IUGG mean radius).
EARTH_RADIUS_KM = 6371.0088

#: One degree of latitude is ~111.32 km anywhere on the globe.
KM_PER_DEG_LAT = 111.32


@dataclass(frozen=True)
class Point:
    """An immutable WGS-84 coordinate pair."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not is_valid_coordinate(self.latitude, self.longitude):
            raise ValueError(
                f"Invalid coordinate: lat={self.latitude}, lng={self.longitude}"
            )

    def as_tuple(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


@dataclass(frozen=True)
class BoundingBox:
    """Axis-aligned lat/lng window used for the SQL prefilter."""

    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float

    def contains(self, latitude: float, longitude: float) -> bool:
        return (
            self.min_lat <= latitude <= self.max_lat
            and self.min_lng <= longitude <= self.max_lng
        )


def is_valid_coordinate(latitude: Optional[float], longitude: Optional[float]) -> bool:
    """Return ``True`` only for a fully specified, in-range coordinate pair."""
    if latitude is None or longitude is None:
        return False
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return False
    if math.isnan(lat) or math.isnan(lng) or math.isinf(lat) or math.isinf(lng):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0


def haversine_km(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
) -> float:
    """Great-circle distance between two points in kilometres.

    Uses the haversine formula, which is numerically stable for the short
    distances (a few hundred km at most) this application deals with.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    # asin(sqrt(a)) is clamped implicitly because a <= 1 by construction.
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def distance_between(a: Point, b: Point) -> float:
    """Haversine distance between two :class:`Point` objects, in km."""
    return haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)


def bounding_box(center: Point, radius_km: float) -> BoundingBox:
    """Smallest lat/lng window fully containing ``radius_km`` around ``center``.

    Longitude degrees shrink with the cosine of the latitude, so the window is
    widened accordingly. Near the poles the cosine approaches zero, so the
    longitude span is clamped to the whole globe to avoid a division blow-up.
    """
    radius_km = max(0.0, float(radius_km))
    lat_delta = radius_km / KM_PER_DEG_LAT

    cos_lat = math.cos(math.radians(center.latitude))
    if abs(cos_lat) < 1e-9:
        lng_delta = 180.0
    else:
        lng_delta = min(180.0, radius_km / (KM_PER_DEG_LAT * abs(cos_lat)))

    return BoundingBox(
        min_lat=max(-90.0, center.latitude - lat_delta),
        max_lat=min(90.0, center.latitude + lat_delta),
        min_lng=max(-180.0, center.longitude - lng_delta),
        max_lng=min(180.0, center.longitude + lng_delta),
    )


def filter_by_bounding_box(queryset, center: Point, radius_km: float):
    """Narrow ``queryset`` to rows whose lat/lng fall inside the radius window.

    Expects the model to expose ``latitude`` / ``longitude`` float fields.
    """
    box = bounding_box(center, radius_km)
    return queryset.filter(
        latitude__gte=box.min_lat,
        latitude__lte=box.max_lat,
        longitude__gte=box.min_lng,
        longitude__lte=box.max_lng,
    )


def annotate_distance(rows: Iterable, center: Point, attr: str = "distance_km") -> list:
    """Attach the exact distance from ``center`` onto each row and sort by it.

    Rows lacking a usable coordinate are dropped, since an un-pinned donor
    cannot participate in a proximity search.
    """
    out = []
    for row in rows:
        if not is_valid_coordinate(
            getattr(row, "latitude", None), getattr(row, "longitude", None)
        ):
            continue
        setattr(
            row,
            attr,
            haversine_km(center.latitude, center.longitude, row.latitude, row.longitude),
        )
        out.append(row)
    out.sort(key=lambda r: getattr(r, attr))
    return out


def within_radius(rows: Iterable, center: Point, radius_km: float) -> list:
    """Exact radius filter: bounding boxes over-select at the corners."""
    return [
        row
        for row in annotate_distance(rows, center)
        if getattr(row, "distance_km") <= radius_km
    ]


def format_distance(km: Optional[float]) -> str:
    """Human friendly distance label for templates."""
    if km is None:
        return "Unknown"
    if km < 1.0:
        return f"{int(round(km * 1000))} m"
    if km < 10.0:
        return f"{km:.1f} km"
    return f"{int(round(km))} km"
