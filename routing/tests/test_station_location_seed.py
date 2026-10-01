import csv
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from routing.management.commands.import_station_locations import (
    SEED_COLUMNS,
)
from routing.models import FuelStation


class StationLocationSeedTests(TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    @staticmethod
    def create_station(opis_id, **overrides):
        values = {
            "name": f"Station {opis_id}",
            "address": f"{opis_id} Test Road",
            "normalized_address": (
                f"{opis_id} test road, test city, TX"
            ),
            "city": "Test City",
            "state": "TX",
            "rack_id": opis_id,
            "retail_price": Decimal("3.25000000"),
        }
        values.update(overrides)

        return FuelStation.objects.create(
            opis_id=opis_id,
            **values,
        )

    def write_seed(self, rows, filename="locations.csv"):
        path = self.directory / filename

        with path.open(
            mode="w",
            encoding="utf-8",
            newline="",
        ) as seed_file:
            writer = csv.DictWriter(
                seed_file,
                fieldnames=SEED_COLUMNS,
            )
            writer.writeheader()
            writer.writerows(rows)

        return path

    @staticmethod
    def seed_row(opis_id, **overrides):
        row = {
            "opis_id": opis_id,
            "longitude": "-97.12345678",
            "latitude": "32.12345678",
            "geocoding_provider": "geoapify",
            "geocoding_status": (
                FuelStation.GeocodingStatus.EXACT
            ),
            "geocoding_confidence": "0.9500",
            "matched_address": (
                f"{opis_id} Test Road, Test City, TX"
            ),
        }
        row.update(overrides)
        return row

    def test_imports_locations_and_is_idempotent(self):
        station = self.create_station(
            101,
            geocoding_job_id="old-job",
            station_retry_attempted_at=timezone.now(),
        )
        seed_path = self.write_seed([self.seed_row(101)])
        first_output = StringIO()

        call_command(
            "import_station_locations",
            file=seed_path,
            stdout=first_output,
        )

        station.refresh_from_db()
        self.assertEqual(
            tuple(station.location.coords),
            (-97.12345678, 32.12345678),
        )
        self.assertEqual(station.location.srid, 4326)
        self.assertEqual(station.geocoding_job_id, "")
        self.assertEqual(
            station.geocoding_provider,
            "geoapify",
        )
        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.EXACT,
        )
        self.assertEqual(
            station.geocoding_confidence,
            Decimal("0.9500"),
        )
        self.assertEqual(
            station.matched_address,
            "101 Test Road, Test City, TX",
        )
        self.assertIsNotNone(station.geocoded_at)
        self.assertIsNone(station.station_retry_attempted_at)
        self.assertIn("Stations updated: 1", first_output.getvalue())

        first_geocoded_at = station.geocoded_at
        first_updated_at = station.updated_at
        second_output = StringIO()

        call_command(
            "import_station_locations",
            file=seed_path,
            stdout=second_output,
        )

        station.refresh_from_db()
        self.assertEqual(station.geocoded_at, first_geocoded_at)
        self.assertEqual(station.updated_at, first_updated_at)
        self.assertIn(
            "Stations unchanged: 1",
            second_output.getvalue(),
        )

    def test_missing_station_prevents_all_updates(self):
        station = self.create_station(101)
        seed_path = self.write_seed(
            [
                self.seed_row(101),
                self.seed_row(999),
            ]
        )

        with self.assertRaisesMessage(
            CommandError,
            "stations that are not in the database: 999",
        ):
            call_command(
                "import_station_locations",
                file=seed_path,
            )

        station.refresh_from_db()
        self.assertIsNone(station.location)
        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.PENDING,
        )

    def test_rejects_untrusted_location(self):
        station = self.create_station(101)
        seed_path = self.write_seed(
            [
                self.seed_row(
                    101,
                    geocoding_status=(
                        FuelStation.GeocodingStatus.CITY_CENTROID
                    ),
                )
            ]
        )

        with self.assertRaisesMessage(
            CommandError,
            "untrusted geocoding_status",
        ):
            call_command(
                "import_station_locations",
                file=seed_path,
            )

        station.refresh_from_db()
        self.assertIsNone(station.location)

    def test_export_import_round_trip(self):
        trusted_station = self.create_station(
            101,
            location=Point(-97.5, 32.5, srid=4326),
            geocoding_provider="geoapify",
            geocoding_status=(
                FuelStation.GeocodingStatus.HIGH_CONFIDENCE
            ),
            geocoding_confidence=Decimal("0.8750"),
            matched_address=(
                "101 Test Road, Test City, TX"
            ),
        )
        self.create_station(
            102,
            location=Point(-98.0, 33.0, srid=4326),
            geocoding_provider="geoapify",
            geocoding_status=(
                FuelStation.GeocodingStatus.CITY_CENTROID
            ),
        )
        output_path = self.directory / "processed" / "seed.csv"
        export_output = StringIO()

        call_command(
            "export_station_locations",
            output=output_path,
            stdout=export_output,
        )

        with output_path.open(
            encoding="utf-8",
            newline="",
        ) as seed_file:
            rows = list(csv.DictReader(seed_file))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["opis_id"], "101")
        self.assertEqual(rows[0]["longitude"], "-97.50000000")
        self.assertEqual(rows[0]["latitude"], "32.50000000")
        self.assertIn(
            "Stations exported: 1",
            export_output.getvalue(),
        )

        trusted_station.location = None
        trusted_station.geocoding_provider = ""
        trusted_station.geocoding_status = (
            FuelStation.GeocodingStatus.PENDING
        )
        trusted_station.geocoding_confidence = None
        trusted_station.matched_address = ""
        trusted_station.save()

        call_command(
            "import_station_locations",
            file=output_path,
        )

        trusted_station.refresh_from_db()
        self.assertEqual(
            tuple(trusted_station.location.coords),
            (-97.5, 32.5),
        )
        self.assertEqual(
            trusted_station.geocoding_status,
            FuelStation.GeocodingStatus.HIGH_CONFIDENCE,
        )
        self.assertEqual(
            trusted_station.geocoding_confidence,
            Decimal("0.8750"),
        )

    def test_export_requires_trusted_locations(self):
        self.create_station(
            101,
            location=Point(-97.5, 32.5, srid=4326),
            geocoding_status=(
                FuelStation.GeocodingStatus.CITY_CENTROID
            ),
        )

        with self.assertRaisesMessage(
            CommandError,
            "does not contain trusted station locations",
        ):
            call_command(
                "export_station_locations",
                output=self.directory / "seed.csv",
            )
