import csv
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from routing.management.commands.export_unmatched_stations import (
    classify_address_pattern,
)
from routing.models import FuelStation


class ExportUnmatchedStationsTests(TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

    @staticmethod
    def create_station(
        opis_id,
        *,
        status=FuelStation.GeocodingStatus.UNMATCHED,
        confidence=None,
        matched_address="",
    ):
        return FuelStation.objects.create(
            opis_id=opis_id,
            name="PILOT TRAVEL CENTER #123",
            address="I-80, EXIT 25",
            normalized_address=(
                "i-80, exit 25, test city, TX"
            ),
            city="Test City",
            state="TX",
            rack_id=opis_id,
            retail_price=Decimal("3.25000000"),
            geocoding_status=status,
            geocoding_confidence=confidence,
            matched_address=matched_address,
        )

    def test_exports_only_unmatched_stations(self):
        self.create_station(
            1,
            confidence=Decimal("0.2500"),
            matched_address="Test City, TX, USA",
        )
        self.create_station(
            2,
            status=FuelStation.GeocodingStatus.EXACT,
            confidence=Decimal("1.0000"),
            matched_address="Pilot Travel Center, TX, USA",
        )

        output_path = (
            Path(self.temporary_directory.name)
            / "reports"
            / "unmatched.csv"
        )
        command_output = StringIO()

        self.call_command(
            output_path,
            command_output,
        )

        with output_path.open(
            encoding="utf-8",
            newline="",
        ) as report_file:
            rows = list(csv.DictReader(report_file))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["opis_id"], "1")
        self.assertEqual(
            rows[0]["cleaned_name"],
            "Pilot Travel Center",
        )
        self.assertEqual(
            rows[0]["failure_category"],
            "low_confidence",
        )
        self.assertEqual(
            rows[0]["address_pattern"],
            "interstate_exit",
        )
        self.assertIn(
            "Stations exported: 1",
            command_output.getvalue(),
        )

    def test_classifies_common_address_patterns(self):
        examples = {
            "I-10, EXIT 25": "interstate_exit",
            "SR-10, EXIT 4": "highway_exit",
            "US-41 & SR-10": "us_route",
            "100 MAIN STREET": "other",
        }

        for address, expected in examples.items():
            with self.subTest(address=address):
                self.assertEqual(
                    classify_address_pattern(address),
                    expected,
                )

    @staticmethod
    def call_command(output_path, stdout):
        call_command(
            "export_unmatched_stations",
            output=output_path,
            stdout=stdout,
        )
