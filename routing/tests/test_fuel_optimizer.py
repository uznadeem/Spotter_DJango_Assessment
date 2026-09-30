from decimal import Decimal

from django.test import SimpleTestCase

from routing.services.fuel_optimizer import (
    RouteNotFeasibleError,
    optimize_fuel_stops,
)
from routing.services.openrouteservice import (
    DrivingRoute,
)
from routing.services.station_search import (
    METERS_PER_MILE,
    RouteStation,
)


class FuelOptimizerTests(SimpleTestCase):
    @staticmethod
    def create_route(distance_miles):
        return DrivingRoute(
            distance_meters=(
                Decimal(str(distance_miles))
                * Decimal(str(METERS_PER_MILE))
            ),
            duration_seconds=36000,
            geometry={
                "type": "LineString",
                "coordinates": [
                    [-100.0, 35.0],
                    [-90.0, 35.0],
                ],
            },
            bbox=(
                -100.0,
                35.0,
                -90.0,
                35.0,
            ),
        )

    @staticmethod
    def create_station(
        opis_id,
        distance_miles,
        price,
        *,
        distance_from_route=0.5,
    ):
        return RouteStation(
            opis_id=opis_id,
            name=f"Station {opis_id}",
            address=f"{opis_id} Test Road",
            city="Test City",
            state="TX",
            retail_price=Decimal(price),
            geocoding_status="exact",
            longitude=-95.0,
            latitude=35.0,
            route_fraction=0.5,
            distance_along_route_miles=(
                distance_miles
            ),
            distance_from_route_miles=(
                distance_from_route
            ),
        )

    def test_route_within_initial_range_needs_no_stop(
        self,
    ):
        route = self.create_route(400)

        plan = optimize_fuel_stops(
            route,
            candidate_stations=(),
        )

        self.assertEqual(plan.stops, ())
        self.assertEqual(
            plan.initial_fuel_gallons,
            Decimal("50"),
        )
        self.assertEqual(
            plan.total_fuel_consumed_gallons,
            Decimal("40"),
        )
        self.assertEqual(
            plan.total_gallons_purchased,
            Decimal("0"),
        )
        self.assertEqual(
            plan.fuel_remaining_gallons,
            Decimal("10"),
        )
        self.assertEqual(
            plan.total_cost,
            Decimal("0"),
        )

    def test_buys_only_fuel_required_to_finish(self):
        route = self.create_route(600)
        station = self.create_station(
            opis_id=1,
            distance_miles=300,
            price="3.25",
        )

        plan = optimize_fuel_stops(
            route,
            candidate_stations=(station,),
        )

        self.assertEqual(len(plan.stops), 1)

        stop = plan.stops[0]

        self.assertEqual(stop.station.opis_id, 1)
        self.assertEqual(
            stop.arrival_fuel_gallons,
            Decimal("20"),
        )
        self.assertEqual(
            stop.gallons_purchased,
            Decimal("10"),
        )
        self.assertEqual(
            stop.departure_fuel_gallons,
            Decimal("30"),
        )
        self.assertEqual(
            stop.stop_cost,
            Decimal("32.50"),
        )
        self.assertEqual(
            plan.total_gallons_purchased,
            Decimal("10"),
        )
        self.assertEqual(
            plan.fuel_remaining_gallons,
            Decimal("0"),
        )
        self.assertEqual(
            plan.total_cost,
            Decimal("32.50"),
        )

    def test_minimizes_stops_before_considering_price(self):
        route = self.create_route(550)
        stations = (
            self.create_station(
                opis_id=1,
                distance_miles=30,
                price="3.00",
            ),
            self.create_station(
                opis_id=2,
                distance_miles=40,
                price="3.10",
            ),
            self.create_station(
                opis_id=3,
                distance_miles=60,
                price="3.20",
            ),
        )

        plan = optimize_fuel_stops(
            route,
            candidate_stations=stations,
        )

        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(
            plan.stops[0].station.opis_id,
            3,
        )
        self.assertEqual(
            plan.stops[0].gallons_purchased,
            Decimal("5"),
        )

    def test_uses_cheapest_feasible_minimum_stop_plan(self):
        route = self.create_route(600)
        stations = (
            self.create_station(
                opis_id=1,
                distance_miles=150,
                price="4.00",
            ),
            self.create_station(
                opis_id=2,
                distance_miles=300,
                price="3.00",
            ),
            self.create_station(
                opis_id=3,
                distance_miles=450,
                price="3.50",
            ),
        )

        plan = optimize_fuel_stops(
            route,
            candidate_stations=stations,
        )

        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(
            plan.stops[0].station.opis_id,
            2,
        )
        self.assertEqual(
            plan.total_cost,
            Decimal("30.00"),
        )

    def test_buys_only_enough_to_reach_cheaper_station(
        self,
    ):
        route = self.create_route(1000)

        expensive_station = self.create_station(
            opis_id=1,
            distance_miles=400,
            price="4.00",
        )
        cheaper_station = self.create_station(
            opis_id=2,
            distance_miles=700,
            price="3.00",
        )

        plan = optimize_fuel_stops(
            route,
            candidate_stations=(
                cheaper_station,
                expensive_station,
            ),
        )

        self.assertEqual(
            [
                stop.station.opis_id
                for stop in plan.stops
            ],
            [1, 2],
        )
        self.assertEqual(
            plan.stops[0].gallons_purchased,
            Decimal("20"),
        )
        self.assertEqual(
            plan.stops[1].gallons_purchased,
            Decimal("30"),
        )
        self.assertEqual(
            plan.total_gallons_purchased,
            Decimal("50"),
        )
        self.assertEqual(
            plan.total_cost,
            Decimal("170.00"),
        )

    def test_uses_current_station_when_future_is_costlier(
        self,
    ):
        route = self.create_route(1000)

        cheaper_station = self.create_station(
            opis_id=1,
            distance_miles=300,
            price="3.00",
        )
        expensive_station = self.create_station(
            opis_id=2,
            distance_miles=700,
            price="4.00",
        )

        plan = optimize_fuel_stops(
            route,
            candidate_stations=(
                cheaper_station,
                expensive_station,
            ),
        )

        self.assertEqual(
            [
                stop.station.opis_id
                for stop in plan.stops
            ],
            [1, 2],
        )
        self.assertEqual(
            plan.stops[0].gallons_purchased,
            Decimal("30"),
        )
        self.assertEqual(
            plan.stops[0].departure_fuel_gallons,
            Decimal("50"),
        )
        self.assertEqual(
            plan.stops[1].gallons_purchased,
            Decimal("20"),
        )
        self.assertEqual(
            plan.total_cost,
            Decimal("170.00"),
        )

    def test_supports_multiple_refueling_stops(self):
        route = self.create_route(1300)

        stations = (
            self.create_station(
                opis_id=1,
                distance_miles=400,
                price="3.50",
            ),
            self.create_station(
                opis_id=2,
                distance_miles=800,
                price="3.00",
            ),
            self.create_station(
                opis_id=3,
                distance_miles=1100,
                price="3.20",
            ),
        )

        plan = optimize_fuel_stops(
            route,
            candidate_stations=stations,
        )

        self.assertEqual(
            [
                stop.station.opis_id
                for stop in plan.stops
            ],
            [1, 2],
        )
        self.assertEqual(
            plan.total_gallons_purchased,
            Decimal("80"),
        )
        self.assertEqual(
            plan.fuel_remaining_gallons,
            Decimal("0"),
        )

    def test_raises_when_route_has_no_reachable_station(
        self,
    ):
        route = self.create_route(600)

        with self.assertRaises(
            RouteNotFeasibleError
        ):
            optimize_fuel_stops(
                route,
                candidate_stations=(),
            )

    def test_raises_when_station_is_too_early(
        self,
    ):
        route = self.create_route(600)
        station = self.create_station(
            opis_id=1,
            distance_miles=50,
            price="3.00",
        )

        with self.assertRaises(
            RouteNotFeasibleError
        ):
            optimize_fuel_stops(
                route,
                candidate_stations=(station,),
            )
