from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, override_settings
from django.urls import reverse


@override_settings(GEOAPIFY_API_KEY="test-geoapify-key")
class RouteDemoTests(SimpleTestCase):
    def setUp(self):
        self.url = reverse("route-demo")

    def test_renders_interactive_route_demo(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            "routing/demo.html",
        )
        self.assertContains(
            response,
            'data-api-url="/api/v1/fuel-route/"',
        )
        self.assertContains(
            response,
            'data-map-api-key="test-geoapify-key"',
        )
        self.assertContains(response, "data-route-form")
        self.assertContains(response, "data-route-map")
        self.assertContains(
            response,
            "leaflet@1.9.4/dist/leaflet.js",
        )
        self.assertContains(
            response,
            "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
        )

    def test_accepts_get_requests_only(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["Allow"], "GET")

    @override_settings(GEOAPIFY_API_KEY="")
    def test_renders_missing_map_key_for_client_handling(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'data-map-api-key=""',
        )

    def test_static_assets_are_discoverable(self):
        self.assertIsNotNone(
            finders.find("routing/demo.css")
        )
        self.assertIsNotNone(
            finders.find("routing/demo.js")
        )
