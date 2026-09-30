from unittest.mock import Mock

import requests
from django.test import SimpleTestCase

from routing.services.openrouteservice import (
    Coordinates,
    LocationNotFoundError,
    OpenRouteServiceClient,
    OpenRouteServiceRateLimitError,
    OpenRouteServiceResponseError,
    OpenRouteServiceTimeoutError,
    RouteNotFoundError,
)


class OpenRouteServiceClientTests(SimpleTestCase):
    def setUp(self):
        self.session = Mock()
        self.client = OpenRouteServiceClient(
            api_key="test-api-key",
            base_url="https://ors.example.test",
            timeout=12,
            session=self.session,
        )

    def response(self, payload, *, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    def test_geocodes_a_usa_location(self):
        self.session.request.return_value = self.response(
            {
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [
                                -96.7969,
                                32.7763,
                            ],
                        },
                        "properties": {
                            "label": "Dallas, TX, USA",
                            "country_a": "USA",
                        },
                    }
                ]
            }
        )

        location = self.client.geocode("  Dallas,   TX  ")

        self.assertEqual(location.query, "Dallas, TX")
        self.assertEqual(
            location.label,
            "Dallas, TX, USA",
        )
        self.assertEqual(
            location.coordinates,
            Coordinates(
                longitude=-96.7969,
                latitude=32.7763,
            ),
        )

        self.session.request.assert_called_once_with(
            method="get",
            url=(
                "https://ors.example.test"
                "/geocode/search"
            ),
            headers={
                "Authorization": "test-api-key",
                "Accept": "application/json",
            },
            params={
                "text": "Dallas, TX",
                "boundary.country": "US",
                "size": 5,
            },
            json=None,
            timeout=12,
        )

    def test_rejects_non_usa_candidates(self):
        self.session.request.return_value = self.response(
            {
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [
                                -79.3832,
                                43.6532,
                            ],
                        },
                        "properties": {
                            "label": "Toronto, Canada",
                            "country_a": "CAN",
                        },
                    }
                ]
            }
        )

        with self.assertRaises(LocationNotFoundError):
            self.client.geocode("Toronto")

    def test_returns_geojson_driving_route(self):
        self.session.request.return_value = self.response(
            {
                "type": "FeatureCollection",
                "bbox": [
                    -96.8,
                    32.7,
                    -87.6,
                    41.9,
                ],
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                [-96.7969, 32.7763],
                                [-92.0000, 37.0000],
                                [-87.6298, 41.8781],
                            ],
                        },
                        "properties": {
                            "summary": {
                                "distance": 1490000,
                                "duration": 50400,
                            }
                        },
                    }
                ],
                "metadata": {
                    "attribution": (
                        "openrouteservice.org | "
                        "OpenStreetMap contributors"
                    )
                },
            }
        )

        start = Coordinates(-96.7969, 32.7763)
        finish = Coordinates(-87.6298, 41.8781)

        route = self.client.get_driving_route(
            start,
            finish,
        )

        self.assertEqual(route.distance_meters, 1490000)
        self.assertEqual(route.duration_seconds, 50400)
        self.assertEqual(
            route.geometry["type"],
            "LineString",
        )
        self.assertEqual(
            route.bbox,
            (-96.8, 32.7, -87.6, 41.9),
        )

        self.session.request.assert_called_once_with(
            method="post",
            url=(
                "https://ors.example.test"
                "/v2/directions/driving-car/geojson"
            ),
            headers={
                "Authorization": "test-api-key",
                "Accept": "application/geo+json",
            },
            params=None,
            json={
                "coordinates": [
                    [-96.7969, 32.7763],
                    [-87.6298, 41.8781],
                ],
                "instructions": False,
            },
            timeout=12,
        )

    def test_rejects_invalid_route_response(self):
        self.session.request.return_value = self.response(
            {
                "features": [],
            }
        )

        with self.assertRaises(
            LocationNotFoundError,
        ):
            self.client.geocode("Unknown Place")

        self.session.reset_mock()
        self.session.request.return_value = self.response(
            {
                "features": [],
            }
        )

        with self.assertRaisesMessage(
            RouteNotFoundError,
            "could not produce a driving route",
        ):
            self.client.get_driving_route(
                Coordinates(-96.7969, 32.7763),
                Coordinates(-87.6298, 41.8781),
            )

    def test_translates_rate_limit_response(self):
        self.session.request.return_value = self.response(
            {},
            status_code=429,
        )

        with self.assertRaises(
            OpenRouteServiceRateLimitError
        ):
            self.client.geocode("Dallas, TX")

    def test_translates_timeout(self):
        self.session.request.side_effect = requests.Timeout

        with self.assertRaises(
            OpenRouteServiceTimeoutError
        ):
            self.client.geocode("Dallas, TX")

    def test_rejects_invalid_json(self):
        response = self.response({})
        response.json.side_effect = ValueError
        self.session.request.return_value = response

        with self.assertRaises(
            OpenRouteServiceResponseError
        ):
            self.client.geocode("Dallas, TX")
