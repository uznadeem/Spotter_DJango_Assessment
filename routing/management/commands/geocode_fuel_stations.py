import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.db.models import Q
from django.utils import timezone

from routing.models import FuelStation
from routing.services.geoapify import (
    MAX_BATCH_SIZE,
    MAX_PRIORITY,
    MIN_PRIORITY,
    GeoapifyClient,
    GeoapifyError,
)


DEFAULT_BATCH_SIZE = 1000
DEFAULT_PRIORITY = 0.5
GEOCODING_PROVIDER = "geoapify"
STATION_RETRY_RADIUS_METERS = 50_000

MINIMUM_CONFIDENCE = Decimal("0.5000")
HIGH_CONFIDENCE = Decimal("0.8000")
EXACT_CONFIDENCE = Decimal("0.9000")
CONFIDENCE_QUANTUM = Decimal("0.0001")

EXACT_RESULT_TYPES = frozenset(
    {
        "amenity",
        "building",
    }
)

CITY_LEVEL_RESULT_TYPES = frozenset(
    {
        "city",
        "locality",
    }
)

REJECTED_RESULT_TYPES = frozenset(
    {
        "postcode",
        "county",
        "state",
        "country",
    }
)

GEOCODING_UPDATE_FIELDS = (
    "location",
    "geocoding_provider",
    "geocoding_status",
    "geocoding_confidence",
    "matched_address",
    "geocoded_at",
    "updated_at",
)

BRAND_ALIASES = (
    (re.compile(r"\bFLYING\s+J\b", re.IGNORECASE), "Flying J"),
    (
        re.compile(r"\bPILOT\b", re.IGNORECASE),
        "Pilot Travel Center",
    ),
    (
        re.compile(r"\bLOVE'?S\b", re.IGNORECASE),
        "Love's Travel Stop",
    ),
    (
        re.compile(r"\bPETRO\b", re.IGNORECASE),
        "Petro Travel Center",
    ),
    (
        re.compile(r"^TA(?:\s|$)", re.IGNORECASE),
        "TA Travel Center",
    ),
    (
        re.compile(r"\bCIRCLE\s+K\b", re.IGNORECASE),
        "Circle K",
    ),
    (
        re.compile(r"\bKWIK\s+TRIP\b", re.IGNORECASE),
        "Kwik Trip",
    ),
    (
        re.compile(r"\bSAPP\s+BROS\b", re.IGNORECASE),
        "Sapp Bros Travel Center",
    ),
    (
        re.compile(r"\bONE9\b", re.IGNORECASE),
        "One9",
    ),
    (
        re.compile(r"\bSPEEDWAY\b", re.IGNORECASE),
        "Speedway",
    ),
    (
        re.compile(r"\bSINCLAIR\b", re.IGNORECASE),
        "Sinclair",
    ),
)


@dataclass(frozen=True)
class GeocodingOutcome:
    location: Point | None
    status: str
    confidence: Decimal | None
    matched_address: str


def normalize_station_name(name):
    """Remove store IDs and normalize well-known fuel brands."""

    normalized_name = " ".join(name.split())

    for pattern, canonical_name in BRAND_ALIASES:
        if pattern.search(normalized_name):
            return canonical_name

    normalized_name = re.sub(
        r"\s+#\s*[A-Z0-9-]+\b",
        "",
        normalized_name,
        flags=re.IGNORECASE,
    )

    return normalized_name.strip()


def build_geocoding_params(station):
    """Build a structured amenity lookup for Geoapify."""

    return {
        "name": normalize_station_name(station.name),
        "city": station.city,
        "state": station.state,
        "country": "United States",
        "type": "amenity",
    }


def build_city_geocoding_params(station):
    """Build a deduplicated city-level fallback lookup."""

    return {
        "city": station.city,
        "state": station.state,
        "country": "United States",
        "type": "city",
    }


def build_proximity_retry_params(station):
    """Build a station lookup constrained around its city point."""

    if station.location is None:
        raise ValueError(
            "A city-centroid location is required for retry."
        )

    params = build_geocoding_params(station)
    params["filter"] = (
        f"circle:{station.location.x:.6f},"
        f"{station.location.y:.6f},"
        f"{STATION_RETRY_RADIUS_METERS}"
    )

    return params


def normalize_locality(value):
    """Normalize city names for tolerant comparisons."""

    return " ".join(
        re.sub(
            r"[^a-z0-9]+",
            " ",
            str(value).casefold(),
        ).split()
    )


def cities_match(station_city, candidate_city):
    """Allow exact and contained city-name matches."""

    expected = normalize_locality(station_city)
    candidate = normalize_locality(candidate_city)

    if not expected or not candidate:
        return True

    return expected in candidate or candidate in expected


def parse_confidence(value):
    """Convert Geoapify confidence into the model's decimal format."""

    try:
        confidence = Decimal(str(value))

        if (
            not confidence.is_finite()
            or confidence < 0
            or confidence > 1
        ):
            return None

        return confidence.quantize(CONFIDENCE_QUANTUM)
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_coordinates(candidate):
    """Return longitude and latitude when both are valid."""

    try:
        latitude = float(candidate["lat"])
        longitude = float(candidate["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    if not isfinite(latitude) or not isfinite(longitude):
        return None

    if not -90 <= latitude <= 90:
        return None

    if not -180 <= longitude <= 180:
        return None

    return longitude, latitude


def get_best_candidate(provider_result):
    """Extract the first geocoding candidate from a batch result."""

    if not isinstance(provider_result, dict):
        return None

    wrapped_result = provider_result.get("result")

    if not isinstance(wrapped_result, dict):
        return None

    candidates = wrapped_result.get("results")

    if not isinstance(candidates, list) or not candidates:
        return None

    candidate = candidates[0]

    if not isinstance(candidate, dict):
        return None

    return candidate


def unmatched_outcome(
    *,
    confidence=None,
    matched_address="",
):
    return GeocodingOutcome(
        location=None,
        status=FuelStation.GeocodingStatus.UNMATCHED,
        confidence=confidence,
        matched_address=matched_address,
    )


def classify_geocoding_result(station, provider_result):
    """Validate and classify one Geoapify station result."""

    candidate = get_best_candidate(provider_result)

    if candidate is None:
        return unmatched_outcome()

    confidence = parse_confidence(
        (candidate.get("rank") or {}).get("confidence")
        if isinstance(candidate.get("rank"), dict)
        else None
    )

    matched_address = str(
        candidate.get("formatted") or ""
    ).strip()[:512]

    country_code = str(
        candidate.get("country_code") or ""
    ).strip().lower()

    state_code = str(
        candidate.get("state_code") or ""
    ).strip().upper()

    if country_code != "us":
        return unmatched_outcome(
            confidence=confidence,
            matched_address=matched_address,
        )

    if state_code != station.state.upper():
        return unmatched_outcome(
            confidence=confidence,
            matched_address=matched_address,
        )

    result_params = provider_result.get("params")
    is_city_fallback = (
        isinstance(result_params, dict)
        and str(
            result_params.get("type") or ""
        ).strip().lower()
        == "city"
    )
    result_filter = (
        str(result_params.get("filter") or "")
        if isinstance(result_params, dict)
        else ""
    )
    is_proximity_retry = (
        isinstance(result_params, dict)
        and str(
            result_params.get("type") or ""
        ).strip().lower()
        == "amenity"
        and result_filter.startswith("circle:")
    )

    candidate_localities = [
        candidate.get(field)
        for field in (
            "name",
            "city",
            "suburb",
            "district",
            "town",
            "village",
            "municipality",
        )
        if candidate.get(field)
    ]

    locality_matches = (
        not candidate_localities
        or any(
            cities_match(station.city, locality)
            for locality in candidate_localities
        )
    )

    if not locality_matches and not is_proximity_retry:
        return unmatched_outcome(
            confidence=confidence,
            matched_address=matched_address,
        )

    coordinates = parse_coordinates(candidate)

    if coordinates is None:
        return unmatched_outcome(
            confidence=confidence,
            matched_address=matched_address,
        )

    if (
        confidence is None
        or confidence < MINIMUM_CONFIDENCE
    ):
        return unmatched_outcome(
            confidence=confidence,
            matched_address=matched_address,
        )

    longitude, latitude = coordinates
    location = Point(
        longitude,
        latitude,
        srid=4326,
    )

    result_type = str(
        candidate.get("result_type") or ""
    ).strip().lower()

    rank = candidate.get("rank")
    match_type = ""

    if isinstance(rank, dict):
        match_type = str(
            rank.get("match_type") or ""
        ).strip().lower()

    if result_type in REJECTED_RESULT_TYPES:
        return unmatched_outcome(
            confidence=confidence,
            matched_address=matched_address,
        )

    if (
        is_city_fallback
        or result_type in CITY_LEVEL_RESULT_TYPES
    ):
        status = (
            FuelStation.GeocodingStatus.CITY_CENTROID
        )
    elif is_proximity_retry and not locality_matches:
        status = FuelStation.GeocodingStatus.APPROXIMATE
    elif (
        confidence >= EXACT_CONFIDENCE
        and result_type in EXACT_RESULT_TYPES
        and match_type == "full_match"
    ):
        status = FuelStation.GeocodingStatus.EXACT
    elif confidence >= HIGH_CONFIDENCE:
        status = (
            FuelStation.GeocodingStatus.HIGH_CONFIDENCE
        )
    else:
        status = FuelStation.GeocodingStatus.APPROXIMATE

    return GeocodingOutcome(
        location=location,
        status=status,
        confidence=confidence,
        matched_address=matched_address,
    )


class Command(BaseCommand):
    help = (
        "Preview pending fuel stations, submit one Geoapify "
        "batch, or retrieve and persist a completed batch."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()

        mode.add_argument(
            "--submit",
            action="store_true",
            help="Submit one batch of pending stations.",
        )
        mode.add_argument(
            "--job-id",
            help=(
                "Retrieve and apply a previously submitted "
                "Geoapify batch."
            ),
        )
        mode.add_argument(
            "--submit-city-fallback",
            action="store_true",
            help=(
                "Submit one deduplicated batch of city-level "
                "lookups for unmatched stations."
            ),
        )
        mode.add_argument(
            "--submit-station-retry",
            action="store_true",
            help=(
                "Retry city-centroid stations within a "
                "50-kilometre search radius."
            ),
        )

        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=(
                "Stations to submit. Maximum: "
                f"{MAX_BATCH_SIZE}."
            ),
        )
        parser.add_argument(
            "--priority",
            type=float,
            default=DEFAULT_PRIORITY,
            help=(
                "Geoapify priority between "
                f"{MIN_PRIORITY} and {MAX_PRIORITY}."
            ),
        )

    def handle(self, *args, **options):
        job_id = options["job_id"]

        if job_id is not None:
            self.retrieve_batch(job_id)
            return

        batch_size = options["batch_size"]
        priority = options["priority"]

        self.validate_submission_options(
            batch_size=batch_size,
            priority=priority,
        )

        if options["submit"]:
            self.submit_batch(
                batch_size=batch_size,
                priority=priority,
            )
            return

        if options["submit_city_fallback"]:
            self.submit_city_fallback_batch(
                batch_size=batch_size,
                priority=priority,
            )
            return

        if options["submit_station_retry"]:
            self.submit_station_retry_batch(
                batch_size=batch_size,
                priority=priority,
            )
            return

        self.preview(batch_size=batch_size)

    def preview(self, *, batch_size):
        pending_stations = self.pending_stations()
        pending_count = pending_stations.count()
        unmatched_stations = self.unmatched_stations()
        unmatched_count = unmatched_stations.count()
        unmatched_city_count = unmatched_stations.values(
            "city",
            "state",
        ).distinct().count()
        city_centroid_count = (
            self.city_centroid_stations().count()
        )

        submitted_job_ids = list(
            FuelStation.objects.filter(
                geocoding_status=(
                    FuelStation.GeocodingStatus.SUBMITTED
                )
            )
            .exclude(geocoding_job_id="")
            .order_by("geocoding_job_id")
            .values_list(
                "geocoding_job_id",
                flat=True,
            )
            .distinct()
        )

        selected_stations = list(
            pending_stations.only(
                "opis_id",
            )[:batch_size]
        )

        output = [
            "Fuel-station geocoding preview.",
            f"Pending stations: {pending_count}",
            f"Unmatched stations: {unmatched_count}",
            (
                "Unmatched city/state fallback groups: "
                f"{unmatched_city_count}"
            ),
            (
                "City-centroid stations ready for retry: "
                f"{city_centroid_count}"
            ),
            (
                "Stations selected for the next batch: "
                f"{len(selected_stations)}"
            ),
            (
                "Submitted jobs awaiting retrieval: "
                f"{len(submitted_job_ids)}"
            ),
        ]

        if selected_stations:
            output.extend(
                (
                    (
                        "First selected OPIS ID: "
                        f"{selected_stations[0].opis_id}"
                    ),
                    (
                        "Last selected OPIS ID: "
                        f"{selected_stations[-1].opis_id}"
                    ),
                )
            )

        if submitted_job_ids:
            output.append(
                "Outstanding job IDs: "
                + ", ".join(submitted_job_ids)
            )

        output.append("No Geoapify request was made.")

        self.stdout.write("\n".join(output))

    def submit_city_fallback_batch(
        self,
        *,
        batch_size,
        priority,
    ):
        unmatched_stations = self.unmatched_stations()
        unmatched_count = unmatched_stations.count()

        if unmatched_count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "There are no unmatched stations requiring "
                    "city-level fallback geocoding."
                )
            )
            return

        representatives = list(
            unmatched_stations.order_by(
                "state",
                "city",
                "opis_id",
            )
            .distinct("state", "city")
            .only(
                "id",
                "opis_id",
                "city",
                "state",
            )[:batch_size]
        )

        inputs = [
            {
                "id": station.opis_id,
                "params": build_city_geocoding_params(
                    station
                ),
            }
            for station in representatives
        ]

        try:
            result = GeoapifyClient().submit_geocoding_batch(
                inputs,
                priority=priority,
            )
        except (GeoapifyError, ValueError) as error:
            raise CommandError(str(error)) from error

        cities_by_state = {}

        for station in representatives:
            cities_by_state.setdefault(
                station.state,
                set(),
            ).add(station.city)

        selected_groups = Q()

        for state, cities in cities_by_state.items():
            selected_groups |= Q(
                state=state,
                city__in=tuple(cities),
            )

        stations_to_update = self.unmatched_stations().filter(
            selected_groups
        )
        expected_station_count = stations_to_update.count()

        try:
            updated_count = stations_to_update.update(
                geocoding_job_id=result.job_id,
                geocoding_provider=GEOCODING_PROVIDER,
                geocoding_status=(
                    FuelStation.GeocodingStatus.SUBMITTED
                ),
                updated_at=timezone.now(),
            )
        except DatabaseError as error:
            raise CommandError(
                "Geoapify accepted city-fallback job "
                f"{result.job_id}, but the database update "
                "failed. Save this job ID before retrying."
            ) from error

        if updated_count != expected_station_count:
            raise CommandError(
                "Geoapify accepted city-fallback job "
                f"{result.job_id}, but only {updated_count} "
                f"of {expected_station_count} stations were "
                "marked as submitted. Save this job ID."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    (
                        "Geoapify city-fallback batch accepted.",
                        f"Job ID: {result.job_id}",
                        (
                            "City/state groups submitted: "
                            f"{len(representatives)}"
                        ),
                        (
                            "Stations covered by the groups: "
                            f"{updated_count}"
                        ),
                        (
                            "Unmatched stations not included: "
                            f"{unmatched_count - updated_count}"
                        ),
                    )
                )
            )
        )

        if result.is_pending:
            self.stdout.write(
                "The job is still processing. Retrieve it "
                "later with:\n"
                "python manage.py geocode_fuel_stations "
                f"--job-id {result.job_id}"
            )
            return

        self.stdout.write(
            "Geoapify completed the city-fallback batch "
            "immediately."
        )
        self.apply_completed_batch(result)

    def submit_station_retry_batch(
        self,
        *,
        batch_size,
        priority,
    ):
        retry_stations = self.city_centroid_stations()
        retry_count = retry_stations.count()

        if retry_count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "There are no city-centroid stations "
                    "requiring a proximity retry."
                )
            )
            return

        stations = list(
            retry_stations.only(
                "id",
                "opis_id",
                "name",
                "city",
                "state",
                "location",
            )[:batch_size]
        )

        inputs = [
            {
                "id": station.opis_id,
                "params": build_proximity_retry_params(
                    station
                ),
            }
            for station in stations
        ]

        try:
            result = GeoapifyClient().submit_geocoding_batch(
                inputs,
                priority=priority,
            )
        except (GeoapifyError, ValueError) as error:
            raise CommandError(str(error)) from error

        station_ids = [station.pk for station in stations]

        try:
            retry_timestamp = timezone.now()
            updated_count = self.city_centroid_stations().filter(
                pk__in=station_ids,
            ).update(
                geocoding_job_id=result.job_id,
                geocoding_provider=GEOCODING_PROVIDER,
                geocoding_status=(
                    FuelStation.GeocodingStatus.SUBMITTED
                ),
                station_retry_attempted_at=retry_timestamp,
                updated_at=retry_timestamp,
            )
        except DatabaseError as error:
            raise CommandError(
                "Geoapify accepted station-retry job "
                f"{result.job_id}, but the database update "
                "failed. Save this job ID before retrying."
            ) from error

        if updated_count != len(stations):
            raise CommandError(
                "Geoapify accepted station-retry job "
                f"{result.job_id}, but only {updated_count} "
                f"of {len(stations)} stations were marked "
                "as submitted. Save this job ID."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    (
                        "Geoapify station-retry batch accepted.",
                        f"Job ID: {result.job_id}",
                        f"Stations submitted: {updated_count}",
                        (
                            "City-centroid stations remaining: "
                            f"{retry_count - updated_count}"
                        ),
                    )
                )
            )
        )

        if result.is_pending:
            self.stdout.write(
                "The job is still processing. Retrieve it "
                "later with:\n"
                "python manage.py geocode_fuel_stations "
                f"--job-id {result.job_id}"
            )
            return

        self.stdout.write(
            "Geoapify completed the station-retry batch "
            "immediately."
        )
        self.apply_completed_batch(result)

    def submit_batch(self, *, batch_size, priority):
        pending_count = self.pending_stations().count()

        if pending_count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "There are no pending stations to geocode."
                )
            )
            return

        stations = list(
            self.pending_stations().only(
                "id",
                "opis_id",
                "name",
                "address",
                "city",
                "state",
            )[:batch_size]
        )

        inputs = [
            {
                "id": station.opis_id,
                "params": build_geocoding_params(station),
            }
            for station in stations
        ]

        try:
            result = GeoapifyClient().submit_geocoding_batch(
                inputs,
                priority=priority,
            )
        except (GeoapifyError, ValueError) as error:
            raise CommandError(str(error)) from error

        station_ids = [
            station.pk
            for station in stations
        ]

        try:
            updated_count = FuelStation.objects.filter(
                pk__in=station_ids,
                geocoding_status=(
                    FuelStation.GeocodingStatus.PENDING
                ),
            ).update(
                geocoding_job_id=result.job_id,
                geocoding_provider=GEOCODING_PROVIDER,
                geocoding_status=(
                    FuelStation.GeocodingStatus.SUBMITTED
                ),
                updated_at=timezone.now(),
            )
        except DatabaseError as error:
            raise CommandError(
                "Geoapify accepted job "
                f"{result.job_id}, but the database update "
                "failed. Save this job ID before retrying."
            ) from error

        if updated_count != len(stations):
            raise CommandError(
                "Geoapify accepted job "
                f"{result.job_id}, but only {updated_count} "
                f"of {len(stations)} stations were marked "
                "as submitted. Save this job ID."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    (
                        "Geoapify batch accepted.",
                        f"Job ID: {result.job_id}",
                        (
                            "Stations submitted: "
                            f"{updated_count}"
                        ),
                        (
                            "Stations still pending: "
                            f"{pending_count - updated_count}"
                        ),
                    )
                )
            )
        )

        if result.is_pending:
            self.stdout.write(
                "The job is still processing. Retrieve it "
                "later with:\n"
                "python manage.py geocode_fuel_stations "
                f"--job-id {result.job_id}"
            )
            return

        self.stdout.write(
            "Geoapify completed the batch immediately."
        )
        self.apply_completed_batch(result)

    def retrieve_batch(self, job_id):
        normalized_job_id = str(job_id).strip()

        if not normalized_job_id:
            raise CommandError(
                "A non-empty Geoapify job ID is required."
            )

        job_exists = FuelStation.objects.filter(
            geocoding_job_id=normalized_job_id,
        ).exists()

        if not job_exists:
            raise CommandError(
                "No fuel stations are associated with "
                f"Geoapify job {normalized_job_id}."
            )

        try:
            result = GeoapifyClient().get_batch_result(
                normalized_job_id
            )
        except (GeoapifyError, ValueError) as error:
            raise CommandError(str(error)) from error

        if result.job_id != normalized_job_id:
            raise CommandError(
                "Geoapify returned a different job ID than "
                "the one requested."
            )

        if result.is_pending:
            self.stdout.write(
                self.style.WARNING(
                    f"Geoapify job {normalized_job_id} is "
                    "still processing. No database records "
                    "were changed."
                )
            )
            return

        self.apply_completed_batch(result)

    def apply_completed_batch(self, result):
        stations = list(
            FuelStation.objects.filter(
                geocoding_job_id=result.job_id,
            ).order_by("opis_id")
        )

        if not stations:
            raise CommandError(
                "No fuel stations are associated with "
                f"Geoapify job {result.job_id}."
            )

        results_by_opis_id = {}
        results_by_city = {}

        for provider_result in result.results:
            result_id = str(
                provider_result.get("id") or ""
            ).strip()

            if not result_id:
                continue

            if result_id in results_by_opis_id:
                raise CommandError(
                    "Geoapify returned duplicate results for "
                    f"input ID {result_id}."
                )

            results_by_opis_id[result_id] = provider_result

            result_params = provider_result.get("params")

            if (
                isinstance(result_params, dict)
                and result_params.get("type") == "city"
            ):
                city_key = (
                    normalize_locality(
                        result_params.get("city", "")
                    ),
                    str(
                        result_params.get("state", "")
                    ).strip().upper(),
                )

                if all(city_key):
                    results_by_city[city_key] = (
                        provider_result
                    )

        timestamp = timezone.now()
        counts = Counter()

        for station in stations:
            provider_result = results_by_opis_id.get(
                str(station.opis_id)
            )

            if provider_result is None:
                provider_result = results_by_city.get(
                    (
                        normalize_locality(station.city),
                        station.state.upper(),
                    )
                )

            outcome = classify_geocoding_result(
                station,
                provider_result,
            )

            preserve_city_fallback = (
                station.location is not None
                and outcome.status
                in {
                    FuelStation.GeocodingStatus.UNMATCHED,
                    FuelStation.GeocodingStatus.CITY_CENTROID,
                }
            )

            if preserve_city_fallback:
                station.geocoding_status = (
                    FuelStation.GeocodingStatus.CITY_CENTROID
                )
            else:
                station.location = outcome.location
                station.geocoding_status = outcome.status
                station.geocoding_confidence = (
                    outcome.confidence
                )
                station.matched_address = (
                    outcome.matched_address
                )
                station.geocoded_at = timestamp

            station.geocoding_provider = GEOCODING_PROVIDER
            station.updated_at = timestamp

            counts[station.geocoding_status] += 1

        try:
            FuelStation.objects.bulk_update(
                stations,
                fields=GEOCODING_UPDATE_FIELDS,
                batch_size=1000,
            )
        except DatabaseError as error:
            raise CommandError(
                "The Geoapify results were retrieved, but "
                "could not be saved to the database."
            ) from error

        self.stdout.write(
            self.style.SUCCESS(
                "\n".join(
                    (
                        "Geoapify results saved.",
                        f"Job ID: {result.job_id}",
                        f"Stations processed: {len(stations)}",
                        (
                            "Exact: "
                            f"{counts[FuelStation.GeocodingStatus.EXACT]}"
                        ),
                        (
                            "High confidence: "
                            f"{counts[FuelStation.GeocodingStatus.HIGH_CONFIDENCE]}"
                        ),
                        (
                            "Approximate: "
                            f"{counts[FuelStation.GeocodingStatus.APPROXIMATE]}"
                        ),
                        (
                            "City centroid: "
                            f"{counts[FuelStation.GeocodingStatus.CITY_CENTROID]}"
                        ),
                        (
                            "Unmatched: "
                            f"{counts[FuelStation.GeocodingStatus.UNMATCHED]}"
                        ),
                    )
                )
            )
        )

    @staticmethod
    def pending_stations():
        return FuelStation.objects.filter(
            geocoding_status=(
                FuelStation.GeocodingStatus.PENDING
            )
        ).order_by("opis_id")

    @staticmethod
    def unmatched_stations():
        return FuelStation.objects.filter(
            geocoding_status=(
                FuelStation.GeocodingStatus.UNMATCHED
            )
        ).order_by("opis_id")

    @staticmethod
    def city_centroid_stations():
        return FuelStation.objects.filter(
            geocoding_status=(
                FuelStation.GeocodingStatus.CITY_CENTROID
            ),
            location__isnull=False,
            station_retry_attempted_at__isnull=True,
        ).order_by("opis_id")

    @staticmethod
    def validate_submission_options(
        *,
        batch_size,
        priority,
    ):
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise CommandError(
                "Batch size must be between "
                f"1 and {MAX_BATCH_SIZE}."
            )

        if not MIN_PRIORITY <= priority <= MAX_PRIORITY:
            raise CommandError(
                "Priority must be between "
                f"{MIN_PRIORITY} and {MAX_PRIORITY}."
            )
