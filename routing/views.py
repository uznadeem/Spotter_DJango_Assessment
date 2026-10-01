import json
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from routing.services.fuel_optimizer import (
    FUEL_EFFICIENCY_MPG,
    MAXIMUM_RANGE_MILES,
    RouteNotFeasibleError,
    optimize_fuel_stops,
)
from routing.services.openrouteservice import (
    LocationNotFoundError,
    OpenRouteServiceAuthenticationError,
    OpenRouteServiceError,
    OpenRouteServiceRateLimitError,
    OpenRouteServiceTimeoutError,
    RouteNotFoundError,
)
from routing.services.routing_service import CachedRoutingService
from routing.services.station_search import find_stations_near_route


logger = logging.getLogger(__name__)

MILES_QUANTUM = Decimal("0.01")
FUEL_QUANTUM = Decimal("0.001")
MONEY_QUANTUM = Decimal("0.01")


class APIError(Exception):
    def __init__(self, *, code, message, status):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@require_GET
def route_demo(request):
    return render(
        request,
        "routing/demo.html",
        {
            "geoapify_api_key": settings.GEOAPIFY_API_KEY,
        },
    )


@csrf_exempt
def fuel_route(request):
    if request.method != "POST":
        response = _error_response(
            code="method_not_allowed",
            message="Only POST requests are supported.",
            status=405,
        )
        response["Allow"] = "POST"
        return response

    try:
        payload = _parse_json_request(request)
        start_query = _parse_location(payload, "start")
        finish_query = _parse_location(payload, "finish")

        routing_service = CachedRoutingService()
        start = _geocode_location(
            routing_service,
            start_query,
            field_name="start",
        )
        finish = _geocode_location(
            routing_service,
            finish_query,
            field_name="finish",
        )
        route = routing_service.get_driving_route(
            start.coordinates,
            finish.coordinates,
        )
        stations = find_stations_near_route(route)
        plan = optimize_fuel_stops(route, stations)
    except APIError as error:
        return _error_response(
            code=error.code,
            message=error.message,
            status=error.status,
        )
    except RouteNotFoundError:
        return _error_response(
            code="route_not_found",
            message=(
                "A driving route could not be found between "
                "the supplied locations."
            ),
            status=422,
        )
    except RouteNotFeasibleError:
        return _error_response(
            code="fuel_plan_not_found",
            message=(
                "The route cannot be completed with the available "
                "fuel stations and a 500-mile vehicle range."
            ),
            status=422,
        )
    except OpenRouteServiceTimeoutError:
        logger.warning(
            "OpenRouteService timed out while planning a route.",
            exc_info=True,
        )
        return _error_response(
            code="routing_service_timeout",
            message="The routing service timed out.",
            status=504,
        )
    except (
        OpenRouteServiceAuthenticationError,
        OpenRouteServiceRateLimitError,
    ):
        logger.warning(
            "OpenRouteService is unavailable while planning a route.",
            exc_info=True,
        )
        return _error_response(
            code="routing_service_unavailable",
            message="The routing service is temporarily unavailable.",
            status=503,
        )
    except OpenRouteServiceError:
        logger.warning(
            "OpenRouteService failed while planning a route.",
            exc_info=True,
        )
        return _error_response(
            code="routing_service_error",
            message="The routing service returned an invalid response.",
            status=502,
        )

    return JsonResponse(
        _build_response(
            start=start,
            finish=finish,
            route=route,
            plan=plan,
        )
    )


def _parse_json_request(request):
    if request.content_type != "application/json":
        raise APIError(
            code="unsupported_media_type",
            message="Content-Type must be application/json.",
            status=415,
        )

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise APIError(
            code="invalid_request",
            message="The request body must contain valid JSON.",
            status=400,
        ) from error

    if not isinstance(payload, dict):
        raise APIError(
            code="invalid_request",
            message="The request body must be a JSON object.",
            status=400,
        )

    return payload


def _parse_location(payload, field_name):
    value = payload.get(field_name)
    display_name = field_name.capitalize()

    if not isinstance(value, str):
        message = (
            f"{display_name} is required."
            if value is None
            else f"{display_name} must be a string."
        )
        raise APIError(
            code="invalid_request",
            message=message,
            status=400,
        )

    normalized_value = " ".join(value.split())

    if not normalized_value:
        raise APIError(
            code="invalid_request",
            message=f"{display_name} is required.",
            status=400,
        )

    if len(normalized_value) > 255:
        raise APIError(
            code="invalid_request",
            message=(
                f"{display_name} cannot exceed 255 characters."
            ),
            status=400,
        )

    return normalized_value


def _geocode_location(service, query, *, field_name):
    try:
        return service.geocode(query)
    except LocationNotFoundError as error:
        raise APIError(
            code="location_not_found",
            message=(
                f"The {field_name} location could not be found "
                "within the USA."
            ),
            status=404,
        ) from error


def _build_response(*, start, finish, route, plan):
    stops = [
        _serialize_stop(sequence, stop)
        for sequence, stop in enumerate(
            plan.stops,
            start=1,
        )
    ]

    return {
        "start": _serialize_location(start),
        "finish": _serialize_location(finish),
        "route": {
            "distance_miles": _decimal_number(
                plan.route_distance_miles,
                MILES_QUANTUM,
            ),
            "duration_minutes": _decimal_number(
                Decimal(str(route.duration_seconds)) / Decimal("60"),
                MILES_QUANTUM,
            ),
            "bbox": list(route.bbox),
            "attribution": route.attribution,
        },
        "fuel_plan": {
            "maximum_range_miles": _decimal_number(
                MAXIMUM_RANGE_MILES,
                MILES_QUANTUM,
            ),
            "fuel_efficiency_mpg": _decimal_number(
                FUEL_EFFICIENCY_MPG,
                MILES_QUANTUM,
            ),
            "starting_tank": "full",
            "initial_fuel_gallons": _decimal_number(
                plan.initial_fuel_gallons,
                FUEL_QUANTUM,
            ),
            "total_fuel_consumed_gallons": _decimal_number(
                plan.total_fuel_consumed_gallons,
                FUEL_QUANTUM,
            ),
            "total_gallons_purchased": _decimal_number(
                plan.total_gallons_purchased,
                FUEL_QUANTUM,
            ),
            "fuel_remaining_gallons": _decimal_number(
                plan.fuel_remaining_gallons,
                FUEL_QUANTUM,
            ),
            "total_cost": _money(plan.total_cost),
            "stops": stops,
        },
        "map": _build_map(
            start=start,
            finish=finish,
            route=route,
            stops=stops,
        ),
    }


def _serialize_location(location):
    return {
        "query": location.query,
        "label": location.label,
        "longitude": location.coordinates.longitude,
        "latitude": location.coordinates.latitude,
    }


def _serialize_stop(sequence, stop):
    station = stop.station

    return {
        "sequence": sequence,
        "opis_id": station.opis_id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "longitude": station.longitude,
        "latitude": station.latitude,
        "distance_along_route_miles": _decimal_number(
            station.distance_along_route_miles,
            MILES_QUANTUM,
        ),
        "distance_from_route_miles": _decimal_number(
            station.distance_from_route_miles,
            MILES_QUANTUM,
        ),
        "price_per_gallon": format(
            station.retail_price,
            ".8f",
        ),
        "arrival_fuel_gallons": _decimal_number(
            stop.arrival_fuel_gallons,
            FUEL_QUANTUM,
        ),
        "gallons_purchased": _decimal_number(
            stop.gallons_purchased,
            FUEL_QUANTUM,
        ),
        "departure_fuel_gallons": _decimal_number(
            stop.departure_fuel_gallons,
            FUEL_QUANTUM,
        ),
        "stop_cost": _money(stop.stop_cost),
    }


def _build_map(*, start, finish, route, stops):
    features = [
        {
            "type": "Feature",
            "geometry": route.geometry,
            "properties": {
                "feature_type": "route",
            },
        },
        _point_feature(
            coordinates=start.coordinates.as_list(),
            properties={
                "feature_type": "start",
                "label": start.label,
            },
        ),
    ]

    features.extend(
        _point_feature(
            coordinates=[
                stop["longitude"],
                stop["latitude"],
            ],
            properties={
                "feature_type": "fuel_stop",
                "sequence": stop["sequence"],
                "opis_id": stop["opis_id"],
                "station_name": stop["name"],
                "price_per_gallon": stop["price_per_gallon"],
                "gallons_purchased": stop["gallons_purchased"],
                "stop_cost": stop["stop_cost"],
            },
        )
        for stop in stops
    )

    features.append(
        _point_feature(
            coordinates=finish.coordinates.as_list(),
            properties={
                "feature_type": "finish",
                "label": finish.label,
            },
        )
    )

    return {
        "type": "FeatureCollection",
        "bbox": list(route.bbox),
        "features": features,
    }


def _point_feature(*, coordinates, properties):
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": coordinates,
        },
        "properties": properties,
    }


def _decimal_number(value, quantum):
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def _money(value):
    return format(
        Decimal(str(value)).quantize(
            MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        ),
        ".2f",
    )


def _error_response(*, code, message, status):
    return JsonResponse(
        {
            "error": {
                "code": code,
                "message": message,
            }
        },
        status=status,
    )
