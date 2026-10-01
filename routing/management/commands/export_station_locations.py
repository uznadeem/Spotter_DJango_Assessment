import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from routing.management.commands.import_station_locations import (
    SEED_COLUMNS,
    TRUSTED_STATUSES,
)
from routing.models import FuelStation


class Command(BaseCommand):
    help = (
        "Export trusted station coordinates as a reproducible seed CSV."
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
        stations = FuelStation.objects.filter(
            location__isnull=False,
            geocoding_status__in=TRUSTED_STATUSES,
        ).order_by("opis_id")

        if not stations.exists():
            raise CommandError(
                "The database does not contain trusted station "
                "locations to export."
            )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        exported_count = 0

        with output_path.open(
            mode="w",
            encoding="utf-8",
            newline="",
        ) as output_file:
            writer = csv.DictWriter(
                output_file,
                fieldnames=SEED_COLUMNS,
            )
            writer.writeheader()

            for station in stations.iterator(chunk_size=1000):
                writer.writerow(
                    {
                        "opis_id": station.opis_id,
                        "longitude": format(
                            station.location.x,
                            ".8f",
                        ),
                        "latitude": format(
                            station.location.y,
                            ".8f",
                        ),
                        "geocoding_provider": (
                            station.geocoding_provider
                        ),
                        "geocoding_status": (
                            station.geocoding_status
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
                    }
                )
                exported_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    (
                        "Station-location seed exported.",
                        f"Stations exported: {exported_count}",
                        f"Output: {output_path}",
                    )
                )
            )
        )
