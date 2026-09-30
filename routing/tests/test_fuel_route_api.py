import json
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse

from routing.services.fuel_optimizer import (
    FuelPlan,
    FuelStop,
    RouteNotFeasibleError,
)
from routing.services.openrouteservice import (
    Coordinates,
    DrivingRoute,
    GeocodedLocation,
    LocationNotFoundError,
    OpenRouteServiceRateLimitError,
    OpenRouteServiceTimeoutError,
    RouteNotFoundError,
)
from routing.services.station_search import (
    METERS_PER_MILE,
    RouteStation,
)


class FuelRouteAPITests(SimpleTestCase):
    def setUp(self):
        self.url = reverse("routing:fuel-route")

        routing_service_patcher = patch(
            "routing.views.CachedRoutingService"
        )
        station_search_patcher = patch(
            "routing.views.find_stations_near_route"
        )
        optimizer_patcher = patch(
            "routing.views.optimize_fuel_stops"
        )

        self.routing_service_class = (
            routing_service_patcher.start()
        )
        self.station_search = station_search_patcher.start()
        self.optimizer = optimizer_patcher.start()

        self.addCleanup(routing_service_patcher.stop)
        self.addCleanup(station_search_patcher.stop)
        self.addCleanup(optimizer_patcher.stop)

        self.routing_service = (
            self.routing_service_class.return_value
        )
        self.start = GeocodedLocation(
            query="Denver, CO",
            label="Denver, CO, USA",
            coordinates=Coordinates(
                longitude=-104.9903,
                latitude=39.7392,
            ),
        )
        self.finish = GeocodedLocation(
            query="Salt Lake City, UT",
            label="Salt Lake City, UT, USA",
            coordinates=Coordinates(
                longitude=-111.891,
                latitude=40.7608,
            ),
        )
        self.route = DrivingRoute(
            distance_meters=(
                536.46 * METERS_PER_MILE
            ),
            duration_seconds=28815,
            geometry={
                "type": "LineString",
                "coordinates": [
                    [-104.9903, 39.7392],
                    [-111.891, 40.7608],
                ],
            },
            bbox=(
                -111.891,
                39.7392,
                -104.9903,
                40.7608,
            ),
            attribution="openrouteservice.org",
        )
        self.station = RouteStation(
            opis_id=71215,
            name="MAVERIK #5319",
            address="100 Test Road",
            city="Test City",
            state="CO",
            retail_price=Decimal("3.15900000"),
            geocoding_status="exact",
            longitude=-105.1234,
            latitude=40.2345,
            route_fraction=0.0936,
            distance_along_route_miles=50.2,
            distance_from_route_miles=0.4,
        )
        self.plan = FuelPlan(
            route_distance_miles=Decimal("536.46"),
            total_fuel_consumed_gallons=Decimal("53.646"),
            initial_fuel_gallons=Decimal("50"),
            total_gallons_purchased=Decimal("3.646"),
            fuel_remaining_gallons=Decimal("0"),
            total_cost=Decimal("11.52"),
            stops=(
                FuelStop(
                    station=self.station,
                    arrival_fuel_gallons=Decimal("44.98"),
                    gallons_purchased=Decimal("3.646"),
                    departure_fuel_gallons=Decimal("48.626"),
                    stop_cost=Decimal("11.52"),
                ),
            ),
        )

        self.routing_service.geocode.side_effect = [
            self.start,
            self.finish,
        ]
        self.routing_service.get_driving_route.return_value = (
            self.route
        )
        self.station_search.return_value = (self.station,)
        self.optimizer.return_value = self.plan

    def post(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_returns_fuel_plan_and_map(self):
        response = self.post(
            {
                "start": "  Denver,   CO ",
                "finish": "Salt Lake City, UT",
            }
        )

        self.assertEqual(response.status_code, 200)

        body = response.json()

        self.assertEqual(
            body["route"]["distance_miles"],
            536.46,
        )
        self.assertEqual(
            body["fuel_plan"]["total_cost"],
            "11.52",
        )
        self.assertEqual(
            body["fuel_plan"]["stops"][0]["opis_id"],
            71215,
        )
        self.assertEqual(
            [
                feature["properties"]["feature_type"]
                for feature in body["map"]["features"]
            ],
            ["route", "start", "fuel_stop", "finish"],
        )
        self.assertEqual(
            body["map"]["features"][2]["geometry"],
            {
                "type": "Point",
                "coordinates": [-105.1234, 40.2345],
            },
        )
        self.assertEqual(
            self.routing_service.geocode.call_args_list[0].args,
            ("Denver, CO",),
        )
        self.station_search.assert_called_once_with(self.route)
        self.optimizer.assert_called_once_with(
            self.route,
            (self.station,),
        )

    def test_rejects_invalid_json(self):
        response = self.client.post(
            self.url,
            data="{invalid",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["code"],
            "invalid_request",
        )

    def test_requires_start(self):
        response = self.post(
            {"finish": "Salt Lake City, UT"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["message"],
            "Start is required.",
        )

    def test_requires_finish(self):
        response = self.post({"start": "Denver, CO"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["message"],
            "Finish is required.",
        )

    def test_rejects_unsupported_content_type(self):
        response = self.client.post(
            self.url,
            data="start=Denver",
            content_type="text/plain",
        )

        self.assertEqual(response.status_code, 415)
        self.assertEqual(
            response.json()["error"]["code"],
            "unsupported_media_type",
        )

    def test_rejects_unsupported_http_method(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["Allow"], "POST")
        self.assertEqual(
            response.json()["error"]["code"],
            "method_not_allowed",
        )

    def test_returns_404_when_location_is_not_found(self):
        self.routing_service.geocode.side_effect = (
            LocationNotFoundError
        )

        response = self.post(
            {
                "start": "Unknown Place",
                "finish": "Salt Lake City, UT",
            }
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"],
            "location_not_found",
        )

    def test_returns_422_when_route_is_not_found(self):
        self.routing_service.get_driving_route.side_effect = (
            RouteNotFoundError
        )

        response = self.post(
            {
                "start": "Denver, CO",
                "finish": "Salt Lake City, UT",
            }
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["error"]["code"],
            "route_not_found",
        )

    def test_returns_422_when_fuel_plan_is_not_feasible(self):
        self.optimizer.side_effect = RouteNotFeasibleError

        response = self.post(
            {
                "start": "Denver, CO",
                "finish": "Salt Lake City, UT",
            }
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["error"]["code"],
            "fuel_plan_not_found",
        )

    def test_returns_504_when_routing_service_times_out(self):
        self.routing_service.geocode.side_effect = (
            OpenRouteServiceTimeoutError
        )

        with self.assertLogs("routing.views", level="WARNING"):
            response = self.post(
                {
                    "start": "Denver, CO",
                    "finish": "Salt Lake City, UT",
                }
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(
            response.json()["error"]["code"],
            "routing_service_timeout",
        )

    def test_returns_503_when_routing_service_is_limited(self):
        self.routing_service.geocode.side_effect = (
            OpenRouteServiceRateLimitError
        )

        with self.assertLogs("routing.views", level="WARNING"):
            response = self.post(
                {
                    "start": "Denver, CO",
                    "finish": "Salt Lake City, UT",
                }
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "routing_service_unavailable",
        )
