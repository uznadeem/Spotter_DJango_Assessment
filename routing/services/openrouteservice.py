from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class OpenRouteServiceError(Exception):
    """Base exception for OpenRouteService failures."""


class OpenRouteServiceAuthenticationError(
    OpenRouteServiceError
):
    """Raised when OpenRouteService rejects the API key."""


class OpenRouteServiceRateLimitError(
    OpenRouteServiceError
):
    """Raised when the ORS quota or rate limit is reached."""


class OpenRouteServiceTimeoutError(
    OpenRouteServiceError
):
    """Raised when an ORS request times out."""


class OpenRouteServiceResponseError(
    OpenRouteServiceError
):
    """Raised when ORS returns an invalid response."""


class LocationNotFoundError(OpenRouteServiceError):
    """Raised when a USA location cannot be resolved."""


class RouteNotFoundError(OpenRouteServiceError):
    """Raised when ORS cannot produce a driving route."""


def normalize_location_query(value):
    """Collapse whitespace while preserving display casing."""

    normalized_value = " ".join(str(value or "").split())

    if not normalized_value:
        raise ValueError("A location is required.")

    if len(normalized_value) > 255:
        raise ValueError(
            "A location cannot exceed 255 characters."
        )

    return normalized_value


def parse_number(value, field_name):
    """Parse and validate a finite numeric value."""

    try:
        parsed_value = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{field_name} must be numeric."
        ) from error

    if not isfinite(parsed_value):
        raise ValueError(
            f"{field_name} must be finite."
        )

    return parsed_value


@dataclass(frozen=True, slots=True)
class Coordinates:
    longitude: float
    latitude: float

    def __post_init__(self):
        longitude = parse_number(
            self.longitude,
            "Longitude",
        )
        latitude = parse_number(
            self.latitude,
            "Latitude",
        )

        if not -180 <= longitude <= 180:
            raise ValueError(
                "Longitude must be between -180 and 180."
            )

        if not -90 <= latitude <= 90:
            raise ValueError(
                "Latitude must be between -90 and 90."
            )

        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(self, "latitude", latitude)

    def as_list(self):
        return [
            self.longitude,
            self.latitude,
        ]


@dataclass(frozen=True, slots=True)
class GeocodedLocation:
    query: str
    label: str
    coordinates: Coordinates

    def __post_init__(self):
        if not self.query:
            raise ValueError(
                "A geocoded location requires a query."
            )

        if not self.label:
            raise ValueError(
                "A geocoded location requires a label."
            )


@dataclass(frozen=True, slots=True)
class DrivingRoute:
    distance_meters: float
    duration_seconds: float
    geometry: dict
    bbox: tuple
    attribution: str = ""

    def __post_init__(self):
        distance = parse_number(
            self.distance_meters,
            "Route distance",
        )
        duration = parse_number(
            self.duration_seconds,
            "Route duration",
        )

        if distance <= 0:
            raise ValueError(
                "Route distance must be positive."
            )

        if duration <= 0:
            raise ValueError(
                "Route duration must be positive."
            )

        if not isinstance(self.geometry, Mapping):
            raise ValueError(
                "Route geometry must be a mapping."
            )

        if self.geometry.get("type") != "LineString":
            raise ValueError(
                "Route geometry must be a LineString."
            )

        raw_coordinates = self.geometry.get("coordinates")

        if (
            not isinstance(raw_coordinates, list)
            or len(raw_coordinates) < 2
        ):
            raise ValueError(
                "Route geometry requires at least two points."
            )

        normalized_coordinates = []

        for coordinate_pair in raw_coordinates:
            coordinates = parse_coordinate_pair(
                coordinate_pair
            )
            normalized_coordinates.append(
                coordinates.as_list()
            )

        if (
            not isinstance(self.bbox, (list, tuple))
            or len(self.bbox) != 4
        ):
            raise ValueError(
                "Route bounding box requires four values."
            )

        normalized_bbox = tuple(
            parse_number(value, "Bounding-box value")
            for value in self.bbox
        )

        min_longitude, min_latitude, max_longitude, max_latitude = (
            normalized_bbox
        )

        if (
            min_longitude > max_longitude
            or min_latitude > max_latitude
        ):
            raise ValueError(
                "Route bounding box is invalid."
            )

        object.__setattr__(
            self,
            "distance_meters",
            distance,
        )
        object.__setattr__(
            self,
            "duration_seconds",
            duration,
        )
        object.__setattr__(
            self,
            "geometry",
            {
                "type": "LineString",
                "coordinates": normalized_coordinates,
            },
        )
        object.__setattr__(
            self,
            "bbox",
            normalized_bbox,
        )


def parse_coordinate_pair(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) < 2
    ):
        raise ValueError(
            "A coordinate must contain longitude and latitude."
        )

    return Coordinates(
        longitude=value[0],
        latitude=value[1],
    )


def is_usa_candidate(properties):
    country_code = str(
        properties.get("country_a")
        or properties.get("country_code")
        or ""
    ).strip().upper()

    country_name = str(
        properties.get("country") or ""
    ).strip().casefold()

    return (
        country_code in {"US", "USA"}
        or country_name
        in {
            "united states",
            "united states of america",
            "usa",
        }
    )


class OpenRouteServiceClient:
    def __init__(
        self,
        *,
        api_key=None,
        base_url=None,
        timeout=None,
        session=None,
    ):
        self.api_key = (
            api_key
            if api_key is not None
            else settings.ORS_API_KEY
        )

        if not self.api_key:
            raise ImproperlyConfigured(
                "ORS_API_KEY is not configured."
            )

        configured_base_url = (
            base_url
            if base_url is not None
            else settings.ORS_BASE_URL
        )

        self.base_url = configured_base_url.rstrip("/")
        self.timeout = (
            timeout
            if timeout is not None
            else settings.HTTP_TIMEOUT_SECONDS
        )
        self.session = session or requests.Session()

    def geocode(self, query):
        normalized_query = normalize_location_query(query)

        response = self._request(
            method="get",
            path="/geocode/search",
            params={
                "text": normalized_query,
                "boundary.country": "US",
                "size": 5,
            },
        )

        payload = self._decode_json(response)
        features = payload.get("features")

        if not isinstance(features, list):
            raise OpenRouteServiceResponseError(
                "OpenRouteService returned an invalid "
                "geocoding response."
            )

        for feature in features:
            location = self._parse_geocoding_feature(
                normalized_query,
                feature,
            )

            if location is not None:
                return location

        raise LocationNotFoundError(
            f'No location in the USA matched "{normalized_query}".'
        )

    def get_driving_route(self, start, finish):
        if not isinstance(start, Coordinates):
            raise TypeError(
                "Start must be a Coordinates instance."
            )

        if not isinstance(finish, Coordinates):
            raise TypeError(
                "Finish must be a Coordinates instance."
            )

        if start == finish:
            raise ValueError(
                "Start and finish coordinates must differ."
            )

        response = self._request(
            method="post",
            path="/v2/directions/driving-car/geojson",
            accept="application/geo+json",
            json={
                "coordinates": [
                    start.as_list(),
                    finish.as_list(),
                ],
                "instructions": False,
            },
        )

        payload = self._decode_json(response)

        try:
            features = payload["features"]
            feature = features[0]
            geometry = feature["geometry"]
            summary = feature["properties"]["summary"]
        except (KeyError, IndexError, TypeError) as error:
            raise RouteNotFoundError(
                "OpenRouteService could not produce "
                "a driving route."
            ) from error

        if not isinstance(summary, Mapping):
            raise OpenRouteServiceResponseError(
                "OpenRouteService returned an invalid "
                "route summary."
            )

        raw_bbox = payload.get("bbox") or feature.get("bbox")

        if raw_bbox is None:
            raw_bbox = self._derive_bbox(geometry)

        metadata = payload.get("metadata")
        attribution = ""

        if isinstance(metadata, Mapping):
            attribution = str(
                metadata.get("attribution") or ""
            ).strip()

        try:
            return DrivingRoute(
                distance_meters=summary["distance"],
                duration_seconds=summary["duration"],
                geometry=geometry,
                bbox=tuple(raw_bbox),
                attribution=attribution,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OpenRouteServiceResponseError(
                "OpenRouteService returned invalid route data."
            ) from error

    def _request(
        self,
        *,
        method,
        path,
        accept="application/json",
        params=None,
        json=None,
    ):
        try:
            response = self.session.request(
                method=method,
                url=f"{self.base_url}{path}",
                headers={
                    "Authorization": self.api_key,
                    "Accept": accept,
                },
                params=params,
                json=json,
                timeout=self.timeout,
            )
        except requests.Timeout as error:
            raise OpenRouteServiceTimeoutError(
                "OpenRouteService did not respond in time."
            ) from error
        except requests.RequestException as error:
            raise OpenRouteServiceError(
                "Could not connect to OpenRouteService."
            ) from error

        if response.status_code in {401, 403}:
            raise OpenRouteServiceAuthenticationError(
                "OpenRouteService rejected the API key."
            )

        if response.status_code == 429:
            raise OpenRouteServiceRateLimitError(
                "OpenRouteService rate limit or quota reached."
            )

        if response.status_code >= 500:
            raise OpenRouteServiceError(
                "OpenRouteService is temporarily unavailable."
            )

        if response.status_code != 200:
            raise OpenRouteServiceError(
                "OpenRouteService rejected the request with "
                f"HTTP status {response.status_code}."
            )

        return response

    @staticmethod
    def _decode_json(response):
        try:
            payload = response.json()
        except ValueError as error:
            raise OpenRouteServiceResponseError(
                "OpenRouteService returned invalid JSON."
            ) from error

        if not isinstance(payload, Mapping):
            raise OpenRouteServiceResponseError(
                "OpenRouteService returned an invalid response."
            )

        return payload

    @staticmethod
    def _parse_geocoding_feature(query, feature):
        if not isinstance(feature, Mapping):
            return None

        properties = feature.get("properties")
        geometry = feature.get("geometry")

        if (
            not isinstance(properties, Mapping)
            or not isinstance(geometry, Mapping)
            or geometry.get("type") != "Point"
            or not is_usa_candidate(properties)
        ):
            return None

        label = str(
            properties.get("label")
            or properties.get("name")
            or ""
        ).strip()

        if not label:
            return None

        try:
            coordinates = parse_coordinate_pair(
                geometry.get("coordinates")
            )
        except ValueError:
            return None

        return GeocodedLocation(
            query=query,
            label=label,
            coordinates=coordinates,
        )

    @staticmethod
    def _derive_bbox(geometry):
        if (
            not isinstance(geometry, Mapping)
            or geometry.get("type") != "LineString"
        ):
            raise OpenRouteServiceResponseError(
                "Route geometry is missing."
            )

        raw_coordinates = geometry.get("coordinates")

        if (
            not isinstance(raw_coordinates, list)
            or len(raw_coordinates) < 2
        ):
            raise OpenRouteServiceResponseError(
                "Route geometry is invalid."
            )

        try:
            coordinates = [
                parse_coordinate_pair(value)
                for value in raw_coordinates
            ]
        except ValueError as error:
            raise OpenRouteServiceResponseError(
                "Route geometry contains invalid coordinates."
            ) from error

        longitudes = [
            coordinate.longitude
            for coordinate in coordinates
        ]
        latitudes = [
            coordinate.latitude
            for coordinate in coordinates
        ]

        return (
            min(longitudes),
            min(latitudes),
            max(longitudes),
            max(latitudes),
        )
