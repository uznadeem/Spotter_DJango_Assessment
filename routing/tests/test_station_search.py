from decimal import Decimal

from django.contrib.gis.geos import Point
from django.test import TestCase

from routing.models import FuelStation
from routing.services.openrouteservice import (
    DrivingRoute,
)
from routing.services.station_search import (
    METERS_PER_MILE,
    find_stations_near_route,
)


class RouteStationSearchTests(TestCase):
    def setUp(self):
        self.route = DrivingRoute(
            distance_meters=600 * METERS_PER_MILE,
            duration_seconds=36000,
            geometry={
                "type": "LineString",
                "coordinates": [
                    [-95.0, 30.0],
                    [-95.0, 40.0],
                ],
            },
            bbox=(
                -95.0,
                30.0,
                -95.0,
                40.0,
            ),
        )

    @staticmethod
    def create_station(
        opis_id,
        longitude,
        latitude,
        *,
        status,
        price="3.25000000",
    ):
        return FuelStation.objects.create(
            opis_id=opis_id,
            name=f"Station {opis_id}",
            address=f"{opis_id} Test Road",
            normalized_address=(
                f"{opis_id} test road, test city, TX"
            ),
            city="Test City",
            state="TX",
            rack_id=opis_id,
            retail_price=Decimal(price),
            location=Point(
                longitude,
                latitude,
                srid=4326,
            ),
            geocoding_provider="geoapify",
            geocoding_status=status,
        )

    def test_returns_only_trusted_stations_in_corridor(
        self,
    ):
        self.create_station(
            1,
            -95.0,
            31.0,
            status=FuelStation.GeocodingStatus.EXACT,
        )
        self.create_station(
            2,
            -94.95,
            35.0,
            status=(
                FuelStation.GeocodingStatus.HIGH_CONFIDENCE
            ),
        )
        self.create_station(
            3,
            -95.0,
            39.0,
            status=(
                FuelStation.GeocodingStatus.APPROXIMATE
            ),
        )

        self.create_station(
            4,
            -94.75,
            36.0,
            status=FuelStation.GeocodingStatus.EXACT,
        )
        self.create_station(
            5,
            -95.0,
            36.0,
            status=FuelStation.GeocodingStatus.UNMATCHED,
        )
        self.create_station(
            6,
            -95.0,
            37.0,
            status=(
                FuelStation.GeocodingStatus.CITY_CENTROID
            ),
        )

        stations = find_stations_near_route(
            self.route
        )

        self.assertEqual(
            [
                station.opis_id
                for station in stations
            ],
            [1, 2, 3],
        )

    def test_orders_stations_and_calculates_metrics(
        self,
    ):
        self.create_station(
            1,
            -95.0,
            31.0,
            status=FuelStation.GeocodingStatus.EXACT,
        )
        self.create_station(
            2,
            -94.95,
            35.0,
            status=(
                FuelStation.GeocodingStatus.HIGH_CONFIDENCE
            ),
        )
        self.create_station(
            3,
            -95.0,
            39.0,
            status=(
                FuelStation.GeocodingStatus.APPROXIMATE
            ),
        )

        with self.assertNumQueries(1):
            stations = find_stations_near_route(
                self.route
            )

        first, middle, last = stations

        self.assertLess(
            first.route_fraction,
            middle.route_fraction,
        )
        self.assertLess(
            middle.route_fraction,
            last.route_fraction,
        )

        self.assertLess(
            first.distance_along_route_miles,
            middle.distance_along_route_miles,
        )
        self.assertLess(
            middle.distance_along_route_miles,
            last.distance_along_route_miles,
        )

        self.assertAlmostEqual(
            first.distance_along_route_miles,
            first.route_fraction * 600,
            places=6,
        )
        self.assertAlmostEqual(
            middle.distance_along_route_miles,
            middle.route_fraction * 600,
            places=6,
        )
        self.assertAlmostEqual(
            last.distance_along_route_miles,
            last.route_fraction * 600,
            places=6,
        )

        self.assertLess(
            first.distance_from_route_miles,
            0.1,
        )
        self.assertGreater(
            middle.distance_from_route_miles,
            2,
        )
        self.assertLess(
            middle.distance_from_route_miles,
            5,
        )
        self.assertLess(
            last.distance_from_route_miles,
            0.1,
        )

    def test_breaks_position_ties_by_price(self):
        self.create_station(
            1,
            -95.0,
            35.0,
            status=FuelStation.GeocodingStatus.EXACT,
            price="3.50000000",
        )
        self.create_station(
            2,
            -95.0,
            35.0,
            status=FuelStation.GeocodingStatus.EXACT,
            price="3.10000000",
        )

        stations = find_stations_near_route(
            self.route
        )

        self.assertEqual(
            [
                station.opis_id
                for station in stations
            ],
            [2, 1],
        )
