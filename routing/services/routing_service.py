import hashlib
import json
import logging
from collections.abc import Mapping

from django.conf import settings
from django.core.cache import cache as default_cache
from redis.exceptions import RedisError

from routing.services.openrouteservice import (
    Coordinates,
    DrivingRoute,
    GeocodedLocation,
    OpenRouteServiceClient,
    normalize_location_query,
)


logger = logging.getLogger(__name__)

CACHE_MISS = object()
GEOCODING_CACHE_PREFIX = "ors:geocode:v1"
ROUTE_CACHE_PREFIX = "ors:route:v1"


class CachedRoutingService:
    def __init__(
        self,
        *,
        client=None,
        cache_backend=None,
        geocoding_timeout=None,
        route_timeout=None,
    ):
        self.client = client or OpenRouteServiceClient()
        self.cache = cache_backend or default_cache
        self.geocoding_timeout = (
            geocoding_timeout
            if geocoding_timeout is not None
            else settings.ORS_GEOCODING_CACHE_TIMEOUT
        )
        self.route_timeout = (
            route_timeout
            if route_timeout is not None
            else settings.ORS_ROUTE_CACHE_TIMEOUT
        )

    def geocode(self, query):
        normalized_query = normalize_location_query(query)
        cache_key = self._geocoding_cache_key(
            normalized_query
        )

        cached_value = self._cache_get(cache_key)

        if cached_value is not CACHE_MISS:
            cached_location = self._deserialize_location(
                cached_value
            )

            if cached_location is not None:
                return cached_location

            self._cache_delete(cache_key)

        location = self.client.geocode(normalized_query)

        self._cache_set(
            cache_key,
            self._serialize_location(location),
            timeout=self.geocoding_timeout,
        )

        return location

    def get_driving_route(self, start, finish):
        if not isinstance(start, Coordinates):
            raise TypeError(
                "Start must be a Coordinates instance."
            )

        if not isinstance(finish, Coordinates):
            raise TypeError(
                "Finish must be a Coordinates instance."
            )

        cache_key = self._route_cache_key(
            start,
            finish,
        )
        cached_value = self._cache_get(cache_key)

        if cached_value is not CACHE_MISS:
            cached_route = self._deserialize_route(
                cached_value
            )

            if cached_route is not None:
                return cached_route

            self._cache_delete(cache_key)

        route = self.client.get_driving_route(
            start,
            finish,
        )

        self._cache_set(
            cache_key,
            self._serialize_route(route),
            timeout=self.route_timeout,
        )

        return route

    @staticmethod
    def _geocoding_cache_key(query):
        normalized_query = query.casefold()
        digest = hashlib.sha256(
            normalized_query.encode("utf-8")
        ).hexdigest()

        return f"{GEOCODING_CACHE_PREFIX}:{digest}"

    @staticmethod
    def _route_cache_key(start, finish):
        cache_input = json.dumps(
            [
                [
                    round(start.longitude, 6),
                    round(start.latitude, 6),
                ],
                [
                    round(finish.longitude, 6),
                    round(finish.latitude, 6),
                ],
            ],
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            cache_input.encode("utf-8")
        ).hexdigest()

        return f"{ROUTE_CACHE_PREFIX}:{digest}"

    def _cache_get(self, key):
        try:
            return self.cache.get(
                key,
                CACHE_MISS,
            )
        except RedisError:
            logger.warning(
                "Redis read failed for routing cache.",
                exc_info=True,
            )
            return CACHE_MISS

    def _cache_set(self, key, value, *, timeout):
        try:
            self.cache.set(
                key,
                value,
                timeout=timeout,
            )
        except RedisError:
            logger.warning(
                "Redis write failed for routing cache.",
                exc_info=True,
            )

    def _cache_delete(self, key):
        try:
            self.cache.delete(key)
        except RedisError:
            logger.warning(
                "Redis delete failed for routing cache.",
                exc_info=True,
            )

    @staticmethod
    def _serialize_location(location):
        return {
            "query": location.query,
            "label": location.label,
            "longitude": location.coordinates.longitude,
            "latitude": location.coordinates.latitude,
        }

    @staticmethod
    def _deserialize_location(value):
        if not isinstance(value, Mapping):
            return None

        try:
            return GeocodedLocation(
                query=str(value["query"]),
                label=str(value["label"]),
                coordinates=Coordinates(
                    longitude=value["longitude"],
                    latitude=value["latitude"],
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _serialize_route(route):
        return {
            "distance_meters": route.distance_meters,
            "duration_seconds": route.duration_seconds,
            "geometry": route.geometry,
            "bbox": list(route.bbox),
            "attribution": route.attribution,
        }

    @staticmethod
    def _deserialize_route(value):
        if not isinstance(value, Mapping):
            return None

        try:
            return DrivingRoute(
                distance_meters=value["distance_meters"],
                duration_seconds=value["duration_seconds"],
                geometry=value["geometry"],
                bbox=tuple(value["bbox"]),
                attribution=str(
                    value.get("attribution") or ""
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None
