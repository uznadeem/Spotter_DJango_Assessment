from unittest.mock import Mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from routing.services.geoapify import (
    GeoapifyClient,
    GeoapifyError,
    GeoapifyRateLimitError,
    GeoapifyResponseError,
)


@override_settings(
    GEOAPIFY_API_KEY="test-api-key",
    GEOAPIFY_BASE_URL="https://api.geoapify.test",
    HTTP_TIMEOUT_SECONDS=10,
)
class GeoapifyClientTests(SimpleTestCase):
    def setUp(self):
        self.session = Mock()
        self.client = GeoapifyClient(
            session=self.session,
        )

    @staticmethod
    def response(*, status_code, payload):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = payload
        return response

    def test_submits_batch_with_station_ids(self):
        self.session.request.return_value = self.response(
            status_code=202,
            payload={
                "id": "job-123",
                "status": "pending",
            },
        )

        result = self.client.submit_geocoding_batch(
            [
                {
                    "id": 7,
                    "text": (
                        "WOODSHED OF BIG CABIN, "
                        "I-44 Exit 283, Big Cabin, OK, USA"
                    ),
                }
            ]
        )

        self.assertEqual(result.job_id, "job-123")
        self.assertTrue(result.is_pending)
        self.assertEqual(result.results, ())

        request = self.session.request.call_args.kwargs

        self.assertEqual(request["method"], "post")
        self.assertEqual(
            request["url"],
            "https://api.geoapify.test/v1/batch",
        )
        self.assertEqual(
            request["params"],
            {"apiKey": "test-api-key"},
        )
        self.assertEqual(request["timeout"], 10)

        payload = request["json"]

        self.assertEqual(
            payload["api"],
            "/v1/geocode/search",
        )
        self.assertEqual(
            payload["params"]["filter"],
            "countrycode:us",
        )
        self.assertEqual(payload["priority"], 0.5)
        self.assertEqual(
            payload["inputs"][0]["id"],
            "7",
        )

    def test_returns_completed_batch_results(self):
        provider_result = {
            "id": "7",
            "result": {
                "results": [
                    {
                        "lat": 36.0,
                        "lon": -95.0,
                        "country_code": "us",
                        "state_code": "OK",
                    }
                ]
            },
        }

        self.session.request.return_value = self.response(
            status_code=200,
            payload={
                "id": "job-123",
                "results": [provider_result],
            },
        )

        result = self.client.get_batch_result("job-123")

        self.assertEqual(result.job_id, "job-123")
        self.assertFalse(result.is_pending)
        self.assertEqual(
            result.results,
            (provider_result,),
        )

        request = self.session.request.call_args.kwargs

        self.assertEqual(request["method"], "get")
        self.assertEqual(
            request["params"],
            {
                "apiKey": "test-api-key",
                "id": "job-123",
            },
        )
        self.assertIsNone(request["json"])

    def test_submits_structured_geocoding_params(self):
        self.session.request.return_value = self.response(
            status_code=202,
            payload={
                "id": "job-structured",
                "status": "pending",
            },
        )

        self.client.submit_geocoding_batch(
            [
                {
                    "id": 7,
                    "params": {
                        "name": "Pilot Travel Center",
                        "city": "Gila Bend",
                        "state": "AZ",
                        "country": "United States",
                        "type": "amenity",
                    },
                }
            ]
        )

        payload = (
            self.session.request.call_args.kwargs["json"]
        )

        self.assertEqual(
            payload["inputs"],
            [
                {
                    "id": "7",
                    "params": {
                        "name": "Pilot Travel Center",
                        "city": "Gila Bend",
                        "state": "AZ",
                        "country": "United States",
                        "type": "amenity",
                    },
                }
            ],
        )

    def test_rejects_input_with_text_and_params(self):
        with self.assertRaisesMessage(
            ValueError,
            "requires exactly one of text or params",
        ):
            self.client.submit_geocoding_batch(
                [
                    {
                        "id": 7,
                        "text": "Pilot Travel Center",
                        "params": {
                            "name": "Pilot Travel Center",
                        },
                    }
                ]
            )

        self.session.request.assert_not_called()

    def test_rejects_empty_batch(self):
        with self.assertRaisesMessage(
            ValueError,
            "At least one geocoding input is required.",
        ):
            self.client.submit_geocoding_batch([])

        self.session.request.assert_not_called()

    def test_rejects_more_than_one_thousand_inputs(self):
        inputs = [
            {
                "id": index,
                "text": f"Station {index}, USA",
            }
            for index in range(1001)
        ]

        with self.assertRaisesMessage(
            ValueError,
            "cannot exceed 1000 inputs",
        ):
            self.client.submit_geocoding_batch(inputs)

        self.session.request.assert_not_called()

    def test_raises_specific_rate_limit_error(self):
        self.session.request.return_value = self.response(
            status_code=429,
            payload={"message": "Quota exceeded"},
        )

        with self.assertRaises(
            GeoapifyRateLimitError
        ):
            self.client.get_batch_result("job-123")

    def test_rejects_invalid_completed_response(self):
        self.session.request.return_value = self.response(
            status_code=200,
            payload={
                "id": "job-123",
            },
        )

        with self.assertRaises(
            GeoapifyResponseError
        ):
            self.client.get_batch_result("job-123")

    def test_does_not_expose_api_key_in_http_error(self):
        self.session.request.return_value = self.response(
            status_code=500,
            payload={"message": "Internal error"},
        )

        with self.assertRaises(GeoapifyError) as context:
            self.client.get_batch_result("job-123")

        self.assertNotIn(
            "test-api-key",
            str(context.exception),
        )


@override_settings(GEOAPIFY_API_KEY="")
class GeoapifyConfigurationTests(SimpleTestCase):
    def test_requires_api_key(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            "GEOAPIFY_API_KEY is not configured.",
        ):
            GeoapifyClient()
