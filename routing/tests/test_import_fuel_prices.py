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

from routing.management.commands.import_fuel_prices import (
    REQUIRED_COLUMNS,
)
from routing.models import FuelStation


class ImportFuelPricesTests(TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)

    def write_csv(
        self,
        rows,
        filename="fuel-prices.csv",
        fieldnames=REQUIRED_COLUMNS,
    ):
        path = self.directory / filename

        with path.open(
            mode="w",
            encoding="utf-8",
            newline="",
        ) as csv_file:
            writer = csv.DictWriter(
                csv_file,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)

        return path

    def import_file(self, path):
        output = StringIO()

        call_command(
            "import_fuel_prices",
            file=path,
            stdout=output,
        )

        return output.getvalue()

    @staticmethod
    def station_row(
        *,
        opis_id,
        price,
        name="TEST TRUCK STOP",
        address="I-80, EXIT 1",
        city="Test City",
        state="TX",
        rack_id=100,
    ):
        return {
            "OPIS Truckstop ID": opis_id,
            "Truckstop Name": name,
            "Address": address,
            "City": city,
            "State": state,
            "Rack ID": rack_id,
            "Retail Price": price,
        }

    def test_imports_us_stations_and_selects_lowest_price(self):
        lower_price_row = self.station_row(
            opis_id=1,
            price="3.10",
        )

        path = self.write_csv(
            [
                self.station_row(
                    opis_id=1,
                    price="3.50",
                ),
                lower_price_row,
                lower_price_row,
                self.station_row(
                    opis_id=2,
                    price="3.20",
                    name="SECOND US STATION",
                    address="I-10, EXIT 2",
                    city="Another City",
                    state="AZ",
                    rack_id=200,
                ),
                self.station_row(
                    opis_id=3,
                    price="4.10",
                    name="CANADIAN STATION",
                    city="Toronto",
                    state="ON",
                    rack_id=300,
                ),
            ]
        )

        output = self.import_file(path)

        self.assertEqual(FuelStation.objects.count(), 2)

        station = FuelStation.objects.get(opis_id=1)

        self.assertEqual(
            station.retail_price,
            Decimal("3.10000000"),
        )
        self.assertEqual(station.source_price_count, 2)
        self.assertEqual(
            station.normalized_address,
            "i-80, exit 1, test city, TX",
        )
        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.PENDING,
        )
        self.assertIsNone(station.location)

        self.assertIn("CSV rows read: 5", output)
        self.assertIn(
            "Exact duplicate rows skipped: 1",
            output,
        )
        self.assertIn("Non-US rows skipped: 1", output)
        self.assertIn("Unique US stations: 2", output)
        self.assertIn("Stations created: 2", output)

    def test_reimporting_unchanged_file_performs_no_writes(self):
        path = self.write_csv(
            [
                self.station_row(
                    opis_id=1,
                    price="3.25",
                )
            ]
        )

        self.import_file(path)

        station = FuelStation.objects.get(opis_id=1)
        original_updated_at = station.updated_at

        output = self.import_file(path)

        station.refresh_from_db()

        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertEqual(
            station.updated_at,
            original_updated_at,
        )
        self.assertIn("Stations created: 0", output)
        self.assertIn("Stations updated: 0", output)
        self.assertIn("Stations unchanged: 1", output)

    def test_price_change_preserves_existing_geocoding(self):
        original_path = self.write_csv(
            [
                self.station_row(
                    opis_id=1,
                    price="3.50",
                )
            ],
            filename="original.csv",
        )

        self.import_file(original_path)

        station = FuelStation.objects.get(opis_id=1)
        station.location = Point(
            -97.0000,
            32.0000,
            srid=4326,
        )
        station.geocoding_provider = "geoapify"
        station.geocoding_status = (
            FuelStation.GeocodingStatus.EXACT
        )
        station.geocoding_confidence = Decimal("0.9500")
        station.matched_address = (
            "I-80 Exit 1, Test City, TX, USA"
        )
        station.geocoded_at = timezone.now()
        station.save()

        changed_path = self.write_csv(
            [
                self.station_row(
                    opis_id=1,
                    price="3.10",
                )
            ],
            filename="changed.csv",
        )

        self.import_file(changed_path)

        station.refresh_from_db()

        self.assertEqual(
            station.retail_price,
            Decimal("3.10000000"),
        )
        self.assertIsNotNone(station.location)
        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.EXACT,
        )
        self.assertEqual(
            station.geocoding_provider,
            "geoapify",
        )

    def test_address_change_resets_existing_geocoding(self):
        original_path = self.write_csv(
            [
                self.station_row(
                    opis_id=1,
                    price="3.50",
                )
            ],
            filename="original.csv",
        )

        self.import_file(original_path)

        station = FuelStation.objects.get(opis_id=1)
        station.location = Point(
            -97.0000,
            32.0000,
            srid=4326,
        )
        station.geocoding_provider = "geoapify"
        station.geocoding_status = (
            FuelStation.GeocodingStatus.EXACT
        )
        station.geocoding_confidence = Decimal("0.9500")
        station.matched_address = (
            "I-80 Exit 1, Test City, TX, USA"
        )
        station.geocoded_at = timezone.now()
        station.save()

        changed_path = self.write_csv(
            [
                self.station_row(
                    opis_id=1,
                    price="3.50",
                    address="I-80, EXIT 25",
                )
            ],
            filename="changed.csv",
        )

        output = self.import_file(changed_path)

        station.refresh_from_db()

        self.assertIsNone(station.location)
        self.assertEqual(station.geocoding_provider, "")
        self.assertEqual(
            station.geocoding_status,
            FuelStation.GeocodingStatus.PENDING,
        )
        self.assertIsNone(station.geocoding_confidence)
        self.assertEqual(station.matched_address, "")
        self.assertIsNone(station.geocoded_at)
        self.assertIn(
            "Stations requiring re-geocoding: 1",
            output,
        )

    def test_rejects_missing_columns(self):
        path = self.write_csv(
            [
                {
                    "OPIS Truckstop ID": 1,
                    "Truckstop Name": "TEST STATION",
                }
            ],
            fieldnames=(
                "OPIS Truckstop ID",
                "Truckstop Name",
            ),
        )

        with self.assertRaisesMessage(
            CommandError,
            "CSV is missing required columns",
        ):
            self.import_file(path)

    def test_rejects_invalid_price(self):
        path = self.write_csv(
            [
                self.station_row(
                    opis_id=1,
                    price="not-a-price",
                )
            ]
        )

        with self.assertRaisesMessage(
            CommandError,
            "Row 2 has an invalid Retail Price.",
        ):
            self.import_file(path)
