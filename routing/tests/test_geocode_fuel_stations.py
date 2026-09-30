from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from routing.management.commands.geocode_fuel_stations import (
    classify_geocoding_result,
    normalize_station_name,
)
from routing.models import FuelStation
from routing.services.geoapify import (
    GeoapifyBatchResult,
    GeoapifyError,
)


CLIENT_PATCH_PATH = (
    "routing.management.commands."
    "geocode_fuel_stations.GeoapifyClient"
)


class GeocodeFuelStationsTests(TestCase):
    @staticmethod
    def create_station(
        opis_id,
        *,
        status=FuelStation.GeocodingStatus.PENDING,
        job_id="",
        city="Test City",
        state="TX",
    ):
        return FuelStation.objects.create(
            opis_id=opis_id,
            name=f"STATION {opis_id}",
            address=f"I-80 EXIT {opis_id}",
            normalized_address=(
                f"i-80 exit {opis_id}, {city.casefold()}, "
                f"{state}"
            ),
            city=city,
            state=state,
            rack_id=opis_id,
            retail_price=Decimal("3.25000000"),
            geocoding_job_id=job_id,
            geocoding_provider=(
                "geoapify"
                if job_id
                else ""
            ),
            geocoding_status=status,
        )

    @staticmethod
    def provider_result(
        opis_id,
        *,
        latitude=32.1000,
        longitude=-97.2000,
        country_code="us",
        state_code="TX",
        city="Test City",
        confidence=0.95,
        result_type="amenity",
        match_type="full_match",
        formatted=None,
        input_params=None,
        candidate_name=None,
    ):
        return {
            "id": str(opis_id),
            "params": (
                input_params
                if input_params is not None
                else {
                    "text": f"STATION {opis_id}, USA",
                }
            ),
            "result": {
                "results": [
                    {
                        "lat": latitude,
                        "lon": longitude,
                        "country_code": country_code,
                        "state_code": state_code,
                        "city": city,
                        "name": (
                            candidate_name
                            or f"Station {opis_id}"
                        ),
                        "formatted": (
                            formatted
                            or (
                                f"Station {opis_id}, "
                                "Test City, TX, USA"
                            )
                        ),
                        "result_type": result_type,
                        "rank": {
                            "confidence": confidence,
                            "match_type": match_type,
                        },
                    }
                ]
            },
        }

    @patch(CLIENT_PATCH_PATH)
    def test_default_mode_only_previews_work(
        self,
        client_class,
    ):
        self.create_station(1)
        self.create_station(2)
        self.create_station(
            3,
            status=FuelStation.GeocodingStatus.SUBMITTED,
            job_id="job-existing",
        )

        output = StringIO()

        call_command(
            "geocode_fuel_stations",
            batch_size=1,
            stdout=output,
        )

        client_class.assert_not_called()

        command_output = output.getvalue()

        self.assertIn(
            "Fuel-station geocoding preview.",
            command_output,
        )
        self.assertIn(
            "Pending stations: 2",
            command_output,
        )
        self.assertIn(
            "Stations selected for the next batch: 1",
            command_output,
        )
        self.assertIn(
            "Submitted jobs awaiting retrieval: 1",
            command_output,
        )
        self.assertIn(
            "Outstanding job IDs: job-existing",
            command_output,
        )
        self.assertIn(
            "No Geoapify request was made.",
            command_output,
        )

    @patch(CLIENT_PATCH_PATH)
    def test_submit_selects_pending_stations_and_tracks_job(
        self,
        client_class,
    ):
        first_station = self.create_station(1)
        second_station = self.create_station(2)

        self.create_station(
            3,
            status=FuelStation.GeocodingStatus.EXACT,
        )

        client = client_class.return_value
        client.submit_geocoding_batch.return_value = (
            GeoapifyBatchResult(
                job_id="job-123",
                is_pending=True,
            )
        )

        output = StringIO()

        call_command(
            "geocode_fuel_stations",
            submit=True,
            batch_size=1,
            priority=0.5,
            stdout=output,
        )

        client.submit_geocoding_batch.assert_called_once_with(
            [
                {
                    "id": 1,
                    "params": {
                        "name": "STATION 1",
                        "city": "Test City",
                        "state": "TX",
                        "country": "United States",
                        "type": "amenity",
                    },
                }
            ],
            priority=0.5,
        )

        first_station.refresh_from_db()
        second_station.refresh_from_db()

        self.assertEqual(
            first_station.geocoding_status,
            FuelStation.GeocodingStatus.SUBMITTED,
        )
        self.assertEqual(
            first_station.geocoding_job_id,
            "job-123",
        )
        self.assertEqual(
            first_station.geocoding_provider,
            "geoapify",
        )

        self.assertEqual(
            second_station.geocoding_status,
            FuelStation.GeocodingStatus.PENDING,
        )
        self.assertEqual(
            second_station.geocoding_job_id,
            "",
        )

        command_output = output.getvalue()

        self.assertIn(
            "Geoapify batch accepted.",
            command_output,
        )
        self.assertIn(
            "Job ID: job-123",
            command_output,
        )
        self.assertIn(
            "Stations submitted: 1",
            command_output,
        )
        self.assertIn(
            "Stations still pending: 1",
            command_output,
        )
        self.assertIn(
            "--job-id job-123",
            command_output,
        )

    @patch(CLIENT_PATCH_PATH)
    def test_pending_job_does_not_change_station(
        self,
        client_class,
    ):
        station = self.create_station(
            1,
            status=FuelStation.GeocodingStatus.SUBMITTED,
            job_id="job-123",
        )

        client = client_class.return_value
        client.get_batch_result.return_value = (
            GeoapifyBatchResult(
                job_id="job-123",
                is_pending=True,
            )
        )

        output = StringIO()

        call_command(
            "geocode_fuel_stations",
            job_id="job-123",
            stdout=output,
        )

        client.get_batch_result.assert_called_once_with(
            "job-123"
        )

        station.refresh_from_db()

        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.SUBMITTED,
        )
        self.assertEqual(
            station.geocoding_job_id,
            "job-123",
        )
        self.assertIsNone(station.location)
        self.assertIsNone(station.geocoded_at)

        self.assertIn(
            "is still processing",
            output.getvalue(),
        )
        self.assertIn(
            "No database records were changed.",
            output.getvalue(),
        )

    @patch(CLIENT_PATCH_PATH)
    def test_completed_job_classifies_and_saves_results(
        self,
        client_class,
    ):
        stations = [
            self.create_station(
                opis_id,
                status=(
                    FuelStation.GeocodingStatus.SUBMITTED
                ),
                job_id="job-123",
            )
            for opis_id in range(1, 7)
        ]

        exact_result = self.provider_result(
            1,
            confidence=0.96,
            result_type="amenity",
            match_type="full_match",
            longitude=-97.1000,
            latitude=32.1000,
        )

        high_confidence_result = self.provider_result(
            2,
            confidence=0.85,
            result_type="street",
            match_type="partial_match",
            longitude=-97.2000,
            latitude=32.2000,
        )

        approximate_result = self.provider_result(
            3,
            confidence=0.65,
            result_type="street",
            match_type="partial_match",
            longitude=-97.3000,
            latitude=32.3000,
        )

        city_result = self.provider_result(
            4,
            confidence=0.99,
            result_type="city",
            longitude=-97.4000,
            latitude=32.4000,
        )

        wrong_state_result = self.provider_result(
            5,
            confidence=0.99,
            result_type="amenity",
            state_code="CA",
            longitude=-117.5000,
            latitude=34.5000,
            formatted="Incorrect Station, CA, USA",
        )

        client = client_class.return_value
        client.get_batch_result.return_value = (
            GeoapifyBatchResult(
                job_id="job-123",
                is_pending=False,
                results=(
                    exact_result,
                    high_confidence_result,
                    approximate_result,
                    city_result,
                    wrong_state_result,
                ),
            )
        )

        output = StringIO()

        call_command(
            "geocode_fuel_stations",
            job_id="job-123",
            stdout=output,
        )

        for station in stations:
            station.refresh_from_db()

        exact_station = stations[0]

        self.assertEqual(
            exact_station.geocoding_status,
            FuelStation.GeocodingStatus.EXACT,
        )
        self.assertEqual(
            exact_station.geocoding_confidence,
            Decimal("0.9600"),
        )
        self.assertIsNotNone(exact_station.location)
        self.assertAlmostEqual(
            exact_station.location.x,
            -97.1000,
        )
        self.assertAlmostEqual(
            exact_station.location.y,
            32.1000,
        )
        self.assertEqual(exact_station.location.srid, 4326)

        high_confidence_station = stations[1]

        self.assertEqual(
            high_confidence_station.geocoding_status,
            FuelStation.GeocodingStatus.HIGH_CONFIDENCE,
        )
        self.assertIsNotNone(
            high_confidence_station.location
        )

        approximate_station = stations[2]

        self.assertEqual(
            approximate_station.geocoding_status,
            FuelStation.GeocodingStatus.APPROXIMATE,
        )
        self.assertIsNotNone(
            approximate_station.location
        )

        city_station = stations[3]

        self.assertEqual(
            city_station.geocoding_status,
            FuelStation.GeocodingStatus.CITY_CENTROID,
        )
        self.assertIsNotNone(city_station.location)

        wrong_state_station = stations[4]

        self.assertEqual(
            wrong_state_station.geocoding_status,
            FuelStation.GeocodingStatus.UNMATCHED,
        )
        self.assertIsNone(wrong_state_station.location)
        self.assertEqual(
            wrong_state_station.matched_address,
            "Incorrect Station, CA, USA",
        )
        self.assertEqual(
            wrong_state_station.geocoding_confidence,
            Decimal("0.9900"),
        )

        missing_result_station = stations[5]

        self.assertEqual(
            missing_result_station.geocoding_status,
            FuelStation.GeocodingStatus.UNMATCHED,
        )
        self.assertIsNone(
            missing_result_station.location
        )
        self.assertIsNone(
            missing_result_station.geocoding_confidence
        )
        self.assertEqual(
            missing_result_station.matched_address,
            "",
        )

        for station in stations:
            self.assertEqual(
                station.geocoding_provider,
                "geoapify",
            )
            self.assertEqual(
                station.geocoding_job_id,
                "job-123",
            )
            self.assertIsNotNone(station.geocoded_at)

        command_output = output.getvalue()

        self.assertIn(
            "Geoapify results saved.",
            command_output,
        )
        self.assertIn(
            "Stations processed: 6",
            command_output,
        )
        self.assertIn("Exact: 1", command_output)
        self.assertIn(
            "High confidence: 1",
            command_output,
        )
        self.assertIn("Approximate: 1", command_output)
        self.assertIn("City centroid: 1", command_output)
        self.assertIn("Unmatched: 2", command_output)

    @patch(CLIENT_PATCH_PATH)
    def test_completed_submission_is_applied_immediately(
        self,
        client_class,
    ):
        station = self.create_station(1)

        client = client_class.return_value
        client.submit_geocoding_batch.return_value = (
            GeoapifyBatchResult(
                job_id="job-immediate",
                is_pending=False,
                results=(
                    self.provider_result(
                        1,
                        confidence=0.97,
                        result_type="building",
                        match_type="full_match",
                    ),
                ),
            )
        )

        output = StringIO()

        call_command(
            "geocode_fuel_stations",
            submit=True,
            batch_size=1,
            stdout=output,
        )

        station.refresh_from_db()

        self.assertEqual(
            station.geocoding_job_id,
            "job-immediate",
        )
        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.EXACT,
        )
        self.assertIsNotNone(station.location)
        self.assertIsNotNone(station.geocoded_at)

        self.assertIn(
            "Geoapify completed the batch immediately.",
            output.getvalue(),
        )
        self.assertIn(
            "Geoapify results saved.",
            output.getvalue(),
        )

    @patch(CLIENT_PATCH_PATH)
    def test_city_fallback_deduplicates_city_groups(
        self,
        client_class,
    ):
        first_station = self.create_station(
            1,
            status=FuelStation.GeocodingStatus.UNMATCHED,
            job_id="old-job",
            city="Alpha City",
        )
        second_station = self.create_station(
            2,
            status=FuelStation.GeocodingStatus.UNMATCHED,
            job_id="old-job",
            city="Alpha City",
        )
        other_city_station = self.create_station(
            3,
            status=FuelStation.GeocodingStatus.UNMATCHED,
            job_id="old-job",
            city="Zulu City",
        )
        pending_station = self.create_station(4)

        client = client_class.return_value
        client.submit_geocoding_batch.return_value = (
            GeoapifyBatchResult(
                job_id="city-job",
                is_pending=True,
            )
        )

        output = StringIO()

        call_command(
            "geocode_fuel_stations",
            submit_city_fallback=True,
            batch_size=1,
            priority=0.5,
            stdout=output,
        )

        client.submit_geocoding_batch.assert_called_once_with(
            [
                {
                    "id": 1,
                    "params": {
                        "city": "Alpha City",
                        "state": "TX",
                        "country": "United States",
                        "type": "city",
                    },
                }
            ],
            priority=0.5,
        )

        for station in (first_station, second_station):
            station.refresh_from_db()
            self.assertEqual(
                station.geocoding_status,
                FuelStation.GeocodingStatus.SUBMITTED,
            )
            self.assertEqual(
                station.geocoding_job_id,
                "city-job",
            )

        other_city_station.refresh_from_db()
        pending_station.refresh_from_db()

        self.assertEqual(
            other_city_station.geocoding_status,
            FuelStation.GeocodingStatus.UNMATCHED,
        )
        self.assertEqual(
            pending_station.geocoding_status,
            FuelStation.GeocodingStatus.PENDING,
        )

        command_output = output.getvalue()
        self.assertIn(
            "City/state groups submitted: 1",
            command_output,
        )
        self.assertIn(
            "Stations covered by the groups: 2",
            command_output,
        )

    @patch(CLIENT_PATCH_PATH)
    def test_city_fallback_result_is_shared_by_city(
        self,
        client_class,
    ):
        first_station = self.create_station(
            1,
            status=FuelStation.GeocodingStatus.UNMATCHED,
            job_id="old-job",
        )
        second_station = self.create_station(
            2,
            status=FuelStation.GeocodingStatus.UNMATCHED,
            job_id="old-job",
        )

        city_params = {
            "city": "Test City",
            "state": "TX",
            "country": "United States",
            "type": "city",
        }

        client = client_class.return_value
        client.submit_geocoding_batch.return_value = (
            GeoapifyBatchResult(
                job_id="city-job",
                is_pending=False,
                results=(
                    self.provider_result(
                        1,
                        confidence=0.99,
                        result_type="suburb",
                        city="Test City",
                        longitude=-97.5,
                        latitude=32.5,
                        input_params=city_params,
                    ),
                ),
            )
        )

        call_command(
            "geocode_fuel_stations",
            submit_city_fallback=True,
            batch_size=1,
        )

        for station in (first_station, second_station):
            station.refresh_from_db()

            self.assertEqual(
                station.geocoding_status,
                FuelStation.GeocodingStatus.CITY_CENTROID,
            )
            self.assertEqual(
                station.geocoding_job_id,
                "city-job",
            )
            self.assertIsNotNone(station.location)
            self.assertAlmostEqual(
                station.location.x,
                -97.5,
            )
            self.assertAlmostEqual(
                station.location.y,
                32.5,
            )

    @patch(CLIENT_PATCH_PATH)
    def test_station_retry_uses_city_centroid_as_search_bias(
        self,
        client_class,
    ):
        station = self.create_station(
            1,
            status=FuelStation.GeocodingStatus.CITY_CENTROID,
            job_id="city-job",
        )
        station.location = Point(-97.5, 32.5, srid=4326)
        station.geocoding_confidence = Decimal("0.9900")
        station.matched_address = "Test City, TX, USA"
        station.geocoded_at = timezone.now()
        station.save(
            update_fields=(
                "location",
                "geocoding_confidence",
                "matched_address",
                "geocoded_at",
            )
        )

        client = client_class.return_value
        client.submit_geocoding_batch.return_value = (
            GeoapifyBatchResult(
                job_id="retry-job",
                is_pending=True,
            )
        )

        output = StringIO()

        call_command(
            "geocode_fuel_stations",
            submit_station_retry=True,
            batch_size=1,
            priority=0.5,
            stdout=output,
        )

        client.submit_geocoding_batch.assert_called_once_with(
            [
                {
                    "id": 1,
                    "params": {
                        "name": "STATION 1",
                        "city": "Test City",
                        "state": "TX",
                        "country": "United States",
                        "type": "amenity",
                        "filter": (
                            "circle:-97.500000,32.500000,50000"
                        ),
                    },
                }
            ],
            priority=0.5,
        )

        station.refresh_from_db()

        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.SUBMITTED,
        )
        self.assertEqual(
            station.geocoding_job_id,
            "retry-job",
        )
        self.assertIsNotNone(
            station.station_retry_attempted_at
        )
        self.assertAlmostEqual(station.location.x, -97.5)
        self.assertAlmostEqual(station.location.y, 32.5)
        self.assertIn(
            "Geoapify station-retry batch accepted.",
            output.getvalue(),
        )

    def test_proximity_retry_accepts_nearby_locality_as_approximate(
        self,
    ):
        station = self.create_station(1)
        provider_result = self.provider_result(
            1,
            city="Nearby City",
            confidence=0.99,
            result_type="amenity",
            match_type="full_match",
            input_params={
                "name": "STATION 1",
                "city": "Test City",
                "state": "TX",
                "country": "United States",
                "type": "amenity",
                "filter": "circle:-97.500000,32.500000,50000",
            },
        )

        outcome = classify_geocoding_result(
            station,
            provider_result,
        )

        self.assertEqual(
            outcome.status,
            FuelStation.GeocodingStatus.APPROXIMATE,
        )
        self.assertIsNotNone(outcome.location)

    @patch(CLIENT_PATCH_PATH)
    def test_failed_station_retry_preserves_city_centroid(
        self,
        client_class,
    ):
        original_geocoded_at = timezone.now()
        station = self.create_station(
            1,
            status=FuelStation.GeocodingStatus.CITY_CENTROID,
            job_id="city-job",
        )
        station.location = Point(-97.5, 32.5, srid=4326)
        station.geocoding_confidence = Decimal("0.9900")
        station.matched_address = "Test City, TX, USA"
        station.geocoded_at = original_geocoded_at
        station.save(
            update_fields=(
                "location",
                "geocoding_confidence",
                "matched_address",
                "geocoded_at",
            )
        )

        client = client_class.return_value
        client.submit_geocoding_batch.return_value = (
            GeoapifyBatchResult(
                job_id="retry-job",
                is_pending=False,
                results=(),
            )
        )

        output = StringIO()

        call_command(
            "geocode_fuel_stations",
            submit_station_retry=True,
            batch_size=1,
            stdout=output,
        )

        station.refresh_from_db()

        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.CITY_CENTROID,
        )
        self.assertAlmostEqual(station.location.x, -97.5)
        self.assertAlmostEqual(station.location.y, 32.5)
        self.assertEqual(
            station.geocoding_confidence,
            Decimal("0.9900"),
        )
        self.assertEqual(
            station.matched_address,
            "Test City, TX, USA",
        )
        self.assertEqual(
            station.geocoded_at,
            original_geocoded_at,
        )
        self.assertIsNotNone(
            station.station_retry_attempted_at
        )
        self.assertIn("City centroid: 1", output.getvalue())

        second_output = StringIO()
        call_command(
            "geocode_fuel_stations",
            submit_station_retry=True,
            stdout=second_output,
        )

        self.assertIn(
            "There are no city-centroid stations requiring "
            "a proximity retry.",
            second_output.getvalue(),
        )
        self.assertEqual(
            client.submit_geocoding_batch.call_count,
            1,
        )

    def test_rejects_candidate_from_different_city(self):
        station = self.create_station(1)

        provider_result = self.provider_result(
            1,
            city="Different City",
            state_code="TX",
            confidence=1,
            result_type="amenity",
            match_type="full_match",
        )

        outcome = classify_geocoding_result(
            station,
            provider_result,
        )

        self.assertEqual(
            outcome.status,
            FuelStation.GeocodingStatus.UNMATCHED,
        )
        self.assertIsNone(outcome.location)

    def test_city_fallback_accepts_matching_locality_name(self):
        station = self.create_station(
            1,
            city="Columbia",
            state="NJ",
        )

        provider_result = self.provider_result(
            1,
            city="Knowlton Township",
            state_code="NJ",
            candidate_name="Columbia",
            confidence=1,
            result_type="suburb",
            input_params={
                "city": "Columbia",
                "state": "NJ",
                "country": "United States",
                "type": "city",
            },
        )

        outcome = classify_geocoding_result(
            station,
            provider_result,
        )

        self.assertEqual(
            outcome.status,
            FuelStation.GeocodingStatus.CITY_CENTROID,
        )
        self.assertIsNotNone(outcome.location)

    @patch(CLIENT_PATCH_PATH)
    def test_provider_failure_leaves_station_pending(
        self,
        client_class,
    ):
        station = self.create_station(1)

        client = client_class.return_value
        client.submit_geocoding_batch.side_effect = (
            GeoapifyError("Geoapify is unavailable.")
        )

        with self.assertRaisesMessage(
            CommandError,
            "Geoapify is unavailable.",
        ):
            call_command(
                "geocode_fuel_stations",
                submit=True,
            )

        station.refresh_from_db()

        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.PENDING,
        )
        self.assertEqual(station.geocoding_job_id, "")
        self.assertEqual(
            station.geocoding_provider,
            "",
        )

    @patch(CLIENT_PATCH_PATH)
    def test_unknown_job_is_rejected_without_api_call(
        self,
        client_class,
    ):
        with self.assertRaisesMessage(
            CommandError,
            (
                "No fuel stations are associated with "
                "Geoapify job unknown-job."
            ),
        ):
            call_command(
                "geocode_fuel_stations",
                job_id="unknown-job",
            )

        client_class.assert_not_called()

    @patch(CLIENT_PATCH_PATH)
    def test_invalid_batch_size_is_rejected_without_api_call(
        self,
        client_class,
    ):
        with self.assertRaisesMessage(
            CommandError,
            "Batch size must be between 1 and 1000.",
        ):
            call_command(
                "geocode_fuel_stations",
                submit=True,
                batch_size=0,
            )

        client_class.assert_not_called()


class StationNameNormalizationTests(TestCase):
    def test_normalizes_known_fuel_brands(self):
        examples = {
            "PILOT TRAVEL CENTER #1243": (
                "Pilot Travel Center"
            ),
            "FLYING J TRAVEL PLAZA #650": "Flying J",
            "LOVES TRAVEL STOP #766": "Love's Travel Stop",
            "TA SAGINAW I 75 TRAVEL CENTER": (
                "TA Travel Center"
            ),
            "PETRO STOPPING CENTER #348": (
                "Petro Travel Center"
            ),
            "CIRCLE K #2612042": "Circle K",
        }

        for source_name, expected_name in examples.items():
            with self.subTest(source_name=source_name):
                self.assertEqual(
                    normalize_station_name(source_name),
                    expected_name,
                )

    def test_removes_store_number_from_unknown_brand(self):
        self.assertEqual(
            normalize_station_name("LOCAL TRUCK STOP #123"),
            "LOCAL TRUCK STOP",
        )
