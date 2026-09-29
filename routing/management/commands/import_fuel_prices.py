import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from routing.models import FuelStation


REQUIRED_COLUMNS = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)

US_STATE_CODES = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    }
)

MUTABLE_IMPORT_FIELDS = (
    "name",
    "address",
    "normalized_address",
    "city",
    "state",
    "rack_id",
    "retail_price",
    "source_price_count",
)

GEOCODING_FIELDS = (
    "location",
    "geocoding_job_id",
    "geocoding_provider",
    "geocoding_status",
    "geocoding_confidence",
    "matched_address",
    "geocoded_at",
)


def clean_text(value):
    """Collapse surrounding and repeated whitespace."""

    return " ".join((value or "").split())


def normalize_address(address, city, state):
    """Create a stable address for grouping and geocoding."""

    return ", ".join(
        (
            clean_text(address).casefold(),
            clean_text(city).casefold(),
            state.upper(),
        )
    )


def parse_positive_number(
    value,
    converter,
    field_name,
    row_number,
):
    """Convert a numeric value and ensure it is finite and positive."""

    try:
        parsed_value = converter(value)

        if (
            isinstance(parsed_value, Decimal)
            and not parsed_value.is_finite()
        ):
            raise ValueError

        if parsed_value <= 0:
            raise ValueError

    except (InvalidOperation, TypeError, ValueError) as error:
        raise CommandError(
            f"Row {row_number} has an invalid {field_name}."
        ) from error

    return parsed_value


class Command(BaseCommand):
    help = (
        "Import the assessment fuel-price CSV, keeping one US station "
        "per OPIS ID and its lowest supplied retail price."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=Path,
            required=True,
            help="Path to the source fuel-price CSV.",
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

        (
            stations,
            total_rows,
            duplicate_rows,
            non_us_rows,
        ) = self.read_source_file(source_path)

        if not stations:
            raise CommandError(
                "The CSV did not contain any valid US stations."
            )

        (
            created_count,
            updated_count,
            unchanged_count,
            reset_count,
        ) = self.persist_stations(stations)

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    (
                        "Fuel-price import completed.",
                        f"CSV rows read: {total_rows}",
                        (
                            "Exact duplicate rows skipped: "
                            f"{duplicate_rows}"
                        ),
                        f"Non-US rows skipped: {non_us_rows}",
                        f"Unique US stations: {len(stations)}",
                        f"Stations created: {created_count}",
                        f"Stations updated: {updated_count}",
                        f"Stations unchanged: {unchanged_count}",
                        (
                            "Stations requiring re-geocoding: "
                            f"{reset_count}"
                        ),
                    )
                )
            )
        )

    def read_source_file(self, source_path):
        stations = {}
        seen_rows = set()

        total_rows = 0
        duplicate_rows = 0
        non_us_rows = 0

        with source_path.open(
            mode="r",
            encoding="utf-8-sig",
            newline="",
        ) as source_file:
            reader = csv.DictReader(source_file)

            missing_columns = set(REQUIRED_COLUMNS) - set(
                reader.fieldnames or ()
            )
            if missing_columns:
                missing = ", ".join(sorted(missing_columns))
                raise CommandError(
                    f"CSV is missing required columns: {missing}"
                )

            for row_number, row in enumerate(reader, start=2):
                total_rows += 1

                cleaned = {
                    column: clean_text(row.get(column))
                    for column in REQUIRED_COLUMNS
                }

                fingerprint = tuple(
                    cleaned[column]
                    for column in REQUIRED_COLUMNS
                )

                if fingerprint in seen_rows:
                    duplicate_rows += 1
                    continue

                seen_rows.add(fingerprint)

                state = cleaned["State"].upper()

                if not state:
                    raise CommandError(
                        f"Row {row_number} has an empty State."
                    )

                if state not in US_STATE_CODES:
                    non_us_rows += 1
                    continue

                required_text_fields = (
                    "Truckstop Name",
                    "Address",
                    "City",
                )
                missing_text_fields = [
                    field
                    for field in required_text_fields
                    if not cleaned[field]
                ]

                if missing_text_fields:
                    missing = ", ".join(missing_text_fields)
                    raise CommandError(
                        f"Row {row_number} has empty values: "
                        f"{missing}"
                    )

                opis_id = parse_positive_number(
                    cleaned["OPIS Truckstop ID"],
                    int,
                    "OPIS Truckstop ID",
                    row_number,
                )

                rack_id = parse_positive_number(
                    cleaned["Rack ID"],
                    int,
                    "Rack ID",
                    row_number,
                )

                retail_price = parse_positive_number(
                    cleaned["Retail Price"],
                    Decimal,
                    "Retail Price",
                    row_number,
                )

                normalized_address = normalize_address(
                    cleaned["Address"],
                    cleaned["City"],
                    state,
                )

                station_data = stations.get(opis_id)

                if station_data is None:
                    stations[opis_id] = {
                        "opis_id": opis_id,
                        "name": cleaned["Truckstop Name"],
                        "address": cleaned["Address"],
                        "normalized_address": normalized_address,
                        "city": cleaned["City"],
                        "state": state,
                        "rack_id": rack_id,
                        "retail_price": retail_price,
                        "source_price_count": 1,
                    }
                    continue

                if (
                    station_data["normalized_address"]
                    != normalized_address
                ):
                    raise CommandError(
                        f"OPIS ID {opis_id} has conflicting "
                        f"addresses at CSV row {row_number}."
                    )

                station_data["source_price_count"] += 1

                if retail_price < station_data["retail_price"]:
                    station_data.update(
                        {
                            "name": cleaned["Truckstop Name"],
                            "address": cleaned["Address"],
                            "city": cleaned["City"],
                            "state": state,
                            "rack_id": rack_id,
                            "retail_price": retail_price,
                        }
                    )

        return (
            stations,
            total_rows,
            duplicate_rows,
            non_us_rows,
        )

    def persist_stations(self, stations):
        timestamp = timezone.now()
        opis_ids = tuple(stations)

        with transaction.atomic():
            existing_by_opis_id = FuelStation.objects.in_bulk(
                opis_ids,
                field_name="opis_id",
            )

            stations_to_create = []
            stations_to_update = []
            stations_to_reset = []
            unchanged_count = 0

            for opis_id, station_data in stations.items():
                existing = existing_by_opis_id.get(opis_id)

                if existing is None:
                    stations_to_create.append(
                        FuelStation(
                            **station_data,
                            created_at=timestamp,
                            updated_at=timestamp,
                        )
                    )
                    continue

                changed = any(
                    getattr(existing, field)
                    != station_data[field]
                    for field in MUTABLE_IMPORT_FIELDS
                )

                if not changed:
                    unchanged_count += 1
                    continue

                address_changed = (
                    existing.normalized_address
                    != station_data["normalized_address"]
                )

                for field in MUTABLE_IMPORT_FIELDS:
                    setattr(
                        existing,
                        field,
                        station_data[field],
                    )

                existing.updated_at = timestamp

                if address_changed:
                    self.reset_geocoding(existing)
                    stations_to_reset.append(existing)
                else:
                    stations_to_update.append(existing)

            if stations_to_create:
                FuelStation.objects.bulk_create(
                    stations_to_create,
                    batch_size=1000,
                )

            if stations_to_update:
                FuelStation.objects.bulk_update(
                    stations_to_update,
                    fields=[
                        *MUTABLE_IMPORT_FIELDS,
                        "updated_at",
                    ],
                    batch_size=1000,
                )

            if stations_to_reset:
                FuelStation.objects.bulk_update(
                    stations_to_reset,
                    fields=[
                        *MUTABLE_IMPORT_FIELDS,
                        "updated_at",
                        *GEOCODING_FIELDS,
                    ],
                    batch_size=1000,
                )

        updated_count = (
            len(stations_to_update)
            + len(stations_to_reset)
        )

        return (
            len(stations_to_create),
            updated_count,
            unchanged_count,
            len(stations_to_reset),
        )

    @staticmethod
    def reset_geocoding(station):
        station.location = None
        station.geocoding_provider = ""
        station.geocoding_job_id = ""
        station.geocoding_status = (
            FuelStation.GeocodingStatus.PENDING
        )
        station.geocoding_confidence = None
        station.matched_address = ""
        station.geocoded_at = None
