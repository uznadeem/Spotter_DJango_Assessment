from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from routing.services.openrouteservice import DrivingRoute
from routing.services.station_search import (
    METERS_PER_MILE,
    RouteStation,
)


MAXIMUM_RANGE_MILES = Decimal("500")
FUEL_EFFICIENCY_MPG = Decimal("10")
TANK_CAPACITY_GALLONS = (
    MAXIMUM_RANGE_MILES / FUEL_EFFICIENCY_MPG
)
MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0")


class RouteNotFeasibleError(Exception):
    """Raised when the vehicle cannot complete the route."""


@dataclass(frozen=True, slots=True)
class FuelStop:
    station: RouteStation
    arrival_fuel_gallons: Decimal
    gallons_purchased: Decimal
    departure_fuel_gallons: Decimal
    stop_cost: Decimal


@dataclass(frozen=True, slots=True)
class FuelPlan:
    route_distance_miles: Decimal
    total_fuel_consumed_gallons: Decimal
    initial_fuel_gallons: Decimal
    total_gallons_purchased: Decimal
    fuel_remaining_gallons: Decimal
    total_cost: Decimal
    stops: tuple[FuelStop, ...]


def optimize_fuel_stops(
    route: DrivingRoute,
    candidate_stations: Iterable[RouteStation],
) -> FuelPlan:
    route_distance_miles = Decimal(
        str(route.distance_meters)
    ) / Decimal(str(METERS_PER_MILE))

    stations = _ordered_route_stations(
        candidate_stations,
        route_distance_miles,
    )

    required_stations = _select_required_stations(
        stations,
        route_distance_miles,
    )

    return _build_fuel_plan(
        route_distance_miles,
        required_stations,
    )


def _select_required_stations(stations, route_distance):
    if route_distance <= MAXIMUM_RANGE_MILES:
        return ()

    minimum_stops_after = [None] * len(stations)

    for index in range(len(stations) - 1, -1, -1):
        _, station_distance = stations[index]

        if route_distance - station_distance <= MAXIMUM_RANGE_MILES:
            minimum_stops_after[index] = 0
            continue

        reachable_stop_counts = [
            1 + minimum_stops_after[next_index]
            for next_index in range(index + 1, len(stations))
            if (
                stations[next_index][1] - station_distance
                <= MAXIMUM_RANGE_MILES
                and minimum_stops_after[next_index] is not None
            )
        ]

        if reachable_stop_counts:
            minimum_stops_after[index] = min(
                reachable_stop_counts
            )

    reachable_from_start = [
        1 + minimum_stops_after[index]
        for index, (_, station_distance) in enumerate(stations)
        if (
            station_distance <= MAXIMUM_RANGE_MILES
            and minimum_stops_after[index] is not None
        )
    ]

    if not reachable_from_start:
        raise RouteNotFeasibleError(
            "The route contains a gap greater than "
            "the vehicle's 500-mile range."
        )

    remaining_stops = min(reachable_from_start)
    current_distance = ZERO
    search_start = 0
    selected_stations = []

    while remaining_stops:
        eligible_stations = []

        for index in range(search_start, len(stations)):
            station, station_distance = stations[index]

            if station_distance <= current_distance:
                continue

            if (
                station_distance - current_distance
                > MAXIMUM_RANGE_MILES
            ):
                break

            if minimum_stops_after[index] == remaining_stops - 1:
                eligible_stations.append(
                    (index, station, station_distance)
                )

        if not eligible_stations:
            raise RouteNotFeasibleError(
                "The route contains a gap greater than "
                "the vehicle's 500-mile range."
            )

        selected_index, station, station_distance = min(
            eligible_stations,
            key=lambda item: (
                item[1].retail_price,
                Decimal(
                    str(
                        item[1].distance_from_route_miles
                    )
                ),
                -item[2],
                item[1].opis_id,
            ),
        )

        selected_stations.append(
            (station, station_distance)
        )
        current_distance = station_distance
        search_start = selected_index + 1
        remaining_stops -= 1

    return tuple(selected_stations)


def _build_fuel_plan(route_distance_miles, stations):
    fuel_remaining = TANK_CAPACITY_GALLONS
    previous_distance = ZERO
    stops = []

    for index, (station, station_distance) in enumerate(
        stations
    ):
        fuel_required = (
            station_distance - previous_distance
        ) / FUEL_EFFICIENCY_MPG

        fuel_remaining -= fuel_required

        if fuel_remaining < ZERO:
            raise RouteNotFeasibleError(
                "The route contains a gap greater than "
                "the vehicle's 500-mile range."
            )

        arrival_fuel = fuel_remaining

        lower_price_distance = (
            _find_next_lower_price_distance(
                stations=stations,
                current_index=index,
                current_distance=station_distance,
                current_price=station.retail_price,
                route_distance=route_distance_miles,
            )
        )

        if lower_price_distance is not None:
            desired_departure_fuel = (
                lower_price_distance - station_distance
            ) / FUEL_EFFICIENCY_MPG
        else:
            desired_departure_fuel = min(
                TANK_CAPACITY_GALLONS,
                (
                    route_distance_miles
                    - station_distance
                )
                / FUEL_EFFICIENCY_MPG,
            )

        gallons_purchased = max(
            ZERO,
            desired_departure_fuel - fuel_remaining,
        )

        if gallons_purchased > ZERO:
            fuel_remaining += gallons_purchased

            stop_cost = (
                gallons_purchased
                * station.retail_price
            ).quantize(
                MONEY_QUANTUM,
                rounding=ROUND_HALF_UP,
            )

            stops.append(
                FuelStop(
                    station=station,
                    arrival_fuel_gallons=arrival_fuel,
                    gallons_purchased=gallons_purchased,
                    departure_fuel_gallons=(
                        fuel_remaining
                    ),
                    stop_cost=stop_cost,
                )
            )

        previous_distance = station_distance

    final_leg_fuel = (
        route_distance_miles - previous_distance
    ) / FUEL_EFFICIENCY_MPG

    fuel_remaining -= final_leg_fuel

    if fuel_remaining < ZERO:
        raise RouteNotFeasibleError(
            "The destination cannot be reached using "
            "the available fuel stations."
        )

    selected_stops = tuple(stops)
    total_gallons_purchased = sum(
        (
            stop.gallons_purchased
            for stop in selected_stops
        ),
        start=ZERO,
    )
    total_cost = sum(
        (
            stop.stop_cost
            for stop in selected_stops
        ),
        start=ZERO,
    )

    return FuelPlan(
        route_distance_miles=route_distance_miles,
        total_fuel_consumed_gallons=(
            route_distance_miles
            / FUEL_EFFICIENCY_MPG
        ),
        initial_fuel_gallons=(
            TANK_CAPACITY_GALLONS
        ),
        total_gallons_purchased=(
            total_gallons_purchased
        ),
        fuel_remaining_gallons=fuel_remaining,
        total_cost=total_cost,
        stops=selected_stops,
    )


def _ordered_route_stations(
    candidate_stations,
    route_distance,
):
    stations = []

    for station in candidate_stations:
        station_distance = Decimal(
            str(station.distance_along_route_miles)
        )

        if ZERO < station_distance < route_distance:
            stations.append(
                (
                    station,
                    station_distance,
                )
            )

    return sorted(
        stations,
        key=lambda item: (
            item[1],
            item[0].retail_price,
            Decimal(
                str(
                    item[0].distance_from_route_miles
                )
            ),
            item[0].opis_id,
        ),
    )


def _find_next_lower_price_distance(
    *,
    stations,
    current_index,
    current_distance,
    current_price,
    route_distance,
):
    for future_station, future_distance in (
        stations[current_index + 1 :]
    ):
        distance_to_station = (
            future_distance - current_distance
        )

        if distance_to_station > MAXIMUM_RANGE_MILES:
            break

        if future_station.retail_price < current_price:
            return future_distance

    distance_to_destination = (
        route_distance - current_distance
    )

    if distance_to_destination <= MAXIMUM_RANGE_MILES:
        return route_distance

    return None
