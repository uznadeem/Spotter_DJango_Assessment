from unittest.mock import Mock, patch

from django.core.cache.backends.locmem import LocMemCache
from django.test import SimpleTestCase
from redis.exceptions import RedisError

from routing.services.openrouteservice import (
    Coordinates,
    DrivingRoute,
    GeocodedLocation,
    OpenRouteServiceClient,
)
from routing.services.routing_service import (
    CachedRoutingService,
)


class CachedRoutingServiceTests(SimpleTestCase):
    def setUp(self):
        self.cache = LocMemCache(
            "routing-service-tests",
            {},
        )
        self.cache.clear()
        self.client = Mock(
            spec=OpenRouteServiceClient
        )
        self.service = CachedRoutingService(
            client=self.client,
            cache_backend=self.cache,
            geocoding_timeout=3600,
            route_timeout=3600,
        )

    def test_geocoding_result_is_cached(self):
        expected_location = GeocodedLocation(
            query="Dallas, TX",
            label="Dallas, TX, USA",
            coordinates=Coordinates(
                longitude=-96.7969,
                latitude=32.7763,
            ),
        )
        self.client.geocode.return_value = (
            expected_location
        )

        first_result = self.service.geocode(
            "  Dallas,   TX "
        )
        second_result = self.service.geocode(
            "dallas, tx"
        )

        self.assertEqual(
            first_result,
            expected_location,
        )
        self.assertEqual(
            second_result,
            expected_location,
        )
        self.client.geocode.assert_called_once_with(
            "Dallas, TX"
        )

    def test_route_result_is_cached(self):
        start = Coordinates(-96.7969, 32.7763)
        finish = Coordinates(-87.6298, 41.8781)
        expected_route = DrivingRoute(
            distance_meters=1490000,
            duration_seconds=50400,
            geometry={
                "type": "LineString",
                "coordinates": [
                    [-96.7969, 32.7763],
                    [-87.6298, 41.8781],
                ],
            },
            bbox=(
                -96.7969,
                32.7763,
                -87.6298,
                41.8781,
            ),
            attribution="OpenRouteService",
        )
        self.client.get_driving_route.return_value = (
            expected_route
        )

        first_result = (
            self.service.get_driving_route(
                start,
                finish,
            )
        )
        second_result = (
            self.service.get_driving_route(
                start,
                finish,
            )
        )

        self.assertEqual(first_result, expected_route)
        self.assertEqual(second_result, expected_route)
        (
            self.client.get_driving_route
            .assert_called_once_with(start, finish)
        )

    def test_route_direction_has_a_different_cache_key(self):
        start = Coordinates(-96.7969, 32.7763)
        finish = Coordinates(-87.6298, 41.8781)
        route = DrivingRoute(
            distance_meters=1490000,
            duration_seconds=50400,
            geometry={
                "type": "LineString",
                "coordinates": [
                    start.as_list(),
                    finish.as_list(),
                ],
            },
            bbox=(
                -96.7969,
                32.7763,
                -87.6298,
                41.8781,
            ),
        )
        self.client.get_driving_route.return_value = route

        self.service.get_driving_route(
            start,
            finish,
        )
        self.service.get_driving_route(
            finish,
            start,
        )

        self.assertEqual(
            self.client.get_driving_route.call_count,
            2,
        )

    @patch("routing.services.routing_service.logger")
    def test_redis_failure_falls_back_to_provider(
        self,
        logger,
    ):
        failing_cache = Mock()
        failing_cache.get.side_effect = RedisError(
            "Redis unavailable"
        )

        service = CachedRoutingService(
            client=self.client,
            cache_backend=failing_cache,
            geocoding_timeout=3600,
            route_timeout=3600,
        )
        expected_location = GeocodedLocation(
            query="Dallas, TX",
            label="Dallas, TX, USA",
            coordinates=Coordinates(
                longitude=-96.7969,
                latitude=32.7763,
            ),
        )
        self.client.geocode.return_value = (
            expected_location
        )

        result = service.geocode("Dallas, TX")

        self.assertEqual(result, expected_location)
        self.client.geocode.assert_called_once_with(
            "Dallas, TX"
        )
        logger.warning.assert_called_once()
