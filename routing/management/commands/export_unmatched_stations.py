import csv
import re
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand

from routing.management.commands.geocode_fuel_stations import (
    normalize_station_name,
)
from routing.models import FuelStation


REPORT_COLUMNS = (
    "opis_id",
    "name",
    "cleaned_name",
    "address",
    "city",
    "state",
    "retail_price",
    "geocoding_confidence",
    "matched_address",
    "failure_category",
    "address_pattern",
)


def classify_failure(station):
    if not station.matched_address:
        return "no_result"

    if station.geocoding_confidence is None:
        return "no_confidence"

    if station.geocoding_confidence < Decimal("0.5000"):
        return "low_confidence"

    return "candidate_rejected"


def classify_address_pattern(address):
    normalized_address = " ".join(address.upper().split())

    if re.search(
        r"\bI-\d+[A-Z]?\b.*\bEXIT\b",
        normalized_address,
    ):
        return "interstate_exit"

    if "EXIT" in normalized_address:
        return "highway_exit"

    if re.search(r"\bUS-\d+[A-Z]?\b", normalized_address):
        return "us_route"

    return "other"


class Command(BaseCommand):
    help = (
        "Export unmatched fuel stations for geocoding "
        "quality analysis without modifying source data."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=Path,
            required=True,
            help="Destination CSV path.",
        )

    def handle(self, *args, **options):
        output_path = options["output"]
        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        stations = FuelStation.objects.filter(
            geocoding_status=(
                FuelStation.GeocodingStatus.UNMATCHED
            )
        ).order_by("opis_id")

        exported_count = 0

        with output_path.open(
            mode="w",
            encoding="utf-8",
            newline="",
        ) as report_file:
            writer = csv.DictWriter(
                report_file,
                fieldnames=REPORT_COLUMNS,
            )
            writer.writeheader()

            for station in stations.iterator(
                chunk_size=1000
            ):
                writer.writerow(
                    {
                        "opis_id": station.opis_id,
                        "name": station.name,
                        "cleaned_name": (
                            normalize_station_name(
                                station.name
                            )
                        ),
                        "address": station.address,
                        "city": station.city,
                        "state": station.state,
                        "retail_price": (
                            station.retail_price
                        ),
                        "geocoding_confidence": (
                            station.geocoding_confidence
                            if (
                                station.geocoding_confidence
                                is not None
                            )
                            else ""
                        ),
                        "matched_address": (
                            station.matched_address
                        ),
                        "failure_category": (
                            classify_failure(station)
                        ),
                        "address_pattern": (
                            classify_address_pattern(
                                station.address
                            )
                        ),
                    }
                )
                exported_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    (
                        "Unmatched-station report exported.",
                        f"Stations exported: {exported_count}",
                        f"Output: {output_path}",
                    )
                )
            )
        )
