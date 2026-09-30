from dataclasses import dataclass
from decimal import Decimal

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import (
    Distance,
    LineLocatePoint,
    Transform,
)
from django.contrib.gis.geos import LineString
from django.contrib.gis.measure import D
from django.db.models import Value
from django.db.models.functions import Cast

from routing.models import FuelStation
from routing.services.openrouteservice import DrivingRoute


METERS_PER_MILE = 1609.344
ROUTE_CORRIDOR_MILES = 5
ROUTE_PROJECTION_SRID = 5070

TRUSTED_GEOCODING_STATUSES = (
    FuelStation.GeocodingStatus.EXACT,
    FuelStation.GeocodingStatus.HIGH_CONFIDENCE,
    FuelStation.GeocodingStatus.APPROXIMATE,
)


@dataclass(frozen=True, slots=True)
class RouteStation:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    retail_price: Decimal
    geocoding_status: str
    longitude: float
    latitude: float
    route_fraction: float
    distance_along_route_miles: float
    distance_from_route_miles: float


def find_stations_near_route(
    route: DrivingRoute,
) -> tuple[RouteStation, ...]:
    route_line = LineString(
        *route.geometry["coordinates"],
        srid=4326,
    )

    route_expression = Value(
        route_line,
        output_field=GeometryField(srid=4326),
    )

    station_geometry = Cast(
        "location",
        output_field=GeometryField(srid=4326),
    )

    projected_route = Transform(
        route_expression,
        ROUTE_PROJECTION_SRID,
    )

    projected_station = Transform(
        station_geometry,
        ROUTE_PROJECTION_SRID,
    )

    rows = (
        FuelStation.objects.filter(
            location__isnull=False,
            geocoding_status__in=(
                TRUSTED_GEOCODING_STATUSES
            ),
            location__dwithin=(
                route_line,
                D(mi=ROUTE_CORRIDOR_MILES),
            ),
        )
        .annotate(
            distance_from_route=Distance(
                "location",
                route_line,
            ),
            route_fraction=LineLocatePoint(
                projected_route,
                projected_station,
            ),
        )
        .values(
            "opis_id",
            "name",
            "address",
            "city",
            "state",
            "retail_price",
            "geocoding_status",
            "location",
            "distance_from_route",
            "route_fraction",
        )
        .order_by(
            "route_fraction",
            "retail_price",
            "opis_id",
        )
    )

    route_distance_miles = (
        route.distance_meters / METERS_PER_MILE
    )

    return tuple(
        RouteStation(
            opis_id=row["opis_id"],
            name=row["name"],
            address=row["address"],
            city=row["city"],
            state=row["state"],
            retail_price=row["retail_price"],
            geocoding_status=(
                row["geocoding_status"]
            ),
            longitude=row["location"].x,
            latitude=row["location"].y,
            route_fraction=row["route_fraction"],
            distance_along_route_miles=(
                row["route_fraction"]
                * route_distance_miles
            ),
            distance_from_route_miles=(
                row["distance_from_route"].m
                / METERS_PER_MILE
            ),
        )
        for row in rows.iterator(chunk_size=500)
    )
