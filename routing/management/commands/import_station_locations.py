import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from routing.models import FuelStation


SEED_COLUMNS = (
    "opis_id",
    "longitude",
    "latitude",
    "geocoding_provider",
    "geocoding_status",
    "geocoding_confidence",
    "matched_address",
)

TRUSTED_STATUSES = frozenset(
    {
        FuelStation.GeocodingStatus.EXACT,
        FuelStation.GeocodingStatus.HIGH_CONFIDENCE,
        FuelStation.GeocodingStatus.APPROXIMATE,
    }
)

UPDATE_FIELDS = (
    "location",
    "geocoding_job_id",
    "geocoding_provider",
    "geocoding_status",
    "geocoding_confidence",
    "matched_address",
    "geocoded_at",
    "station_retry_attempted_at",
    "updated_at",
)


def parse_positive_integer(value, field_name, row_number):
    try:
        parsed_value = int(value)
    except (TypeError, ValueError) as error:
        raise CommandError(
            f"Row {row_number} has an invalid {field_name}."
        ) from error

    if parsed_value <= 0:
        raise CommandError(
            f"Row {row_number} has an invalid {field_name}."
        )

    return parsed_value


def parse_coordinate(
    value,
    field_name,
    row_number,
    *,
    minimum,
    maximum,
):
    try:
        parsed_value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CommandError(
            f"Row {row_number} has an invalid {field_name}."
        ) from error

    if (
        not parsed_value.is_finite()
        or not minimum <= parsed_value <= maximum
    ):
        raise CommandError(
            f"Row {row_number} has an invalid {field_name}."
        )

    return float(parsed_value)


def parse_confidence(value, row_number):
    if not value:
        return None

    try:
        confidence = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CommandError(
            f"Row {row_number} has invalid geocoding_confidence."
        ) from error

    if (
        not confidence.is_finite()
        or not Decimal("0") <= confidence <= Decimal("1")
    ):
        raise CommandError(
            f"Row {row_number} has invalid geocoding_confidence."
        )

    return confidence


class Command(BaseCommand):
    help = (
        "Import trusted station coordinates from a processed seed CSV "
        "without calling a geocoding provider."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=Path,
            required=True,
            help="Path to the processed station-location CSV.",
        )

    def handle(self, *args, **options):
        source_path = options["file"]

        if not source_path.exists():
            raise CommandError(
                f"File does not exist: {source_path}"
            )

        if not source_path.is_file():
            raise CommandError(
                f"Path is not a file: {source_path}"
            )

        locations = self.read_source_file(source_path)

        if not locations:
            raise CommandError(
                "The seed CSV does not contain any station locations."
            )

        updated_count, unchanged_count = self.persist_locations(
            locations
        )

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    (
                        "Station-location import completed.",
                        f"Locations read: {len(locations)}",
                        f"Stations updated: {updated_count}",
                        f"Stations unchanged: {unchanged_count}",
                    )
                )
            )
        )

    def read_source_file(self, source_path):
        locations = {}

        with source_path.open(
            mode="r",
            encoding="utf-8-sig",
            newline="",
        ) as source_file:
            reader = csv.DictReader(source_file)
            missing_columns = set(SEED_COLUMNS) - set(
                reader.fieldnames or ()
            )

            if missing_columns:
                missing = ", ".join(sorted(missing_columns))
                raise CommandError(
                    f"CSV is missing required columns: {missing}"
                )

            for row_number, row in enumerate(reader, start=2):
                opis_id = parse_positive_integer(
                    row.get("opis_id"),
                    "opis_id",
                    row_number,
                )

                if opis_id in locations:
                    raise CommandError(
                        f"Row {row_number} contains duplicate "
                        f"opis_id {opis_id}."
                    )

                longitude = parse_coordinate(
                    row.get("longitude"),
                    "longitude",
                    row_number,
                    minimum=Decimal("-180"),
                    maximum=Decimal("180"),
                )
                latitude = parse_coordinate(
                    row.get("latitude"),
                    "latitude",
                    row_number,
                    minimum=Decimal("-90"),
                    maximum=Decimal("90"),
                )

                provider = " ".join(
                    (row.get("geocoding_provider") or "").split()
                )
                status = (
                    row.get("geocoding_status") or ""
                ).strip()
                confidence = parse_confidence(
                    (
                        row.get("geocoding_confidence")
                        or ""
                    ).strip(),
                    row_number,
                )
                matched_address = " ".join(
                    (row.get("matched_address") or "").split()
                )

                if not provider or len(provider) > 50:
                    raise CommandError(
                        f"Row {row_number} has an invalid "
                        "geocoding_provider."
                    )

                if status not in TRUSTED_STATUSES:
                    raise CommandError(
                        f"Row {row_number} has an untrusted "
                        "geocoding_status."
                    )

                if len(matched_address) > 512:
                    raise CommandError(
                        f"Row {row_number} has an invalid "
                        "matched_address."
                    )

                locations[opis_id] = {
                    "longitude": longitude,
                    "latitude": latitude,
                    "geocoding_provider": provider,
                    "geocoding_status": status,
                    "geocoding_confidence": confidence,
                    "matched_address": matched_address,
                }

        return locations

    def persist_locations(self, locations):
        timestamp = timezone.now()
        existing_by_opis_id = FuelStation.objects.in_bulk(
            locations,
            field_name="opis_id",
        )
        missing_opis_ids = sorted(
            set(locations) - set(existing_by_opis_id)
        )

        if missing_opis_ids:
            preview = ", ".join(
                str(opis_id)
                for opis_id in missing_opis_ids[:10]
            )
            suffix = "..." if len(missing_opis_ids) > 10 else ""
            raise CommandError(
                "Seed locations reference stations that are not in "
                f"the database: {preview}{suffix}. Run "
                "import_fuel_prices first."
            )

        stations_to_update = []
        unchanged_count = 0

        for opis_id, location_data in locations.items():
            station = existing_by_opis_id[opis_id]

            if self.location_matches(station, location_data):
                unchanged_count += 1
                continue

            station.location = Point(
                location_data["longitude"],
                location_data["latitude"],
                srid=4326,
            )
            station.geocoding_job_id = ""
            station.geocoding_provider = location_data[
                "geocoding_provider"
            ]
            station.geocoding_status = location_data[
                "geocoding_status"
            ]
            station.geocoding_confidence = location_data[
                "geocoding_confidence"
            ]
            station.matched_address = location_data[
                "matched_address"
            ]
            station.geocoded_at = timestamp
            station.station_retry_attempted_at = None
            station.updated_at = timestamp
            stations_to_update.append(station)

        with transaction.atomic():
            if stations_to_update:
                FuelStation.objects.bulk_update(
                    stations_to_update,
                    fields=UPDATE_FIELDS,
                    batch_size=1000,
                )

        return len(stations_to_update), unchanged_count

    @staticmethod
    def location_matches(station, location_data):
        return (
            station.location is not None
            and station.location.x == location_data["longitude"]
            and station.location.y == location_data["latitude"]
            and station.geocoding_provider
            == location_data["geocoding_provider"]
            and station.geocoding_status
            == location_data["geocoding_status"]
            and station.geocoding_confidence
            == location_data["geocoding_confidence"]
            and station.matched_address
            == location_data["matched_address"]
        )
