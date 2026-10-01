# Fuel Route API

A Django API that calculates a driving route between two locations in the
United States and recommends cost-effective fuel stops along that route.

The application assumes a vehicle with a 500-mile range, fuel economy of
10 miles per gallon, and a full starting tank. It returns trip and fuel-cost
details together with a GeoJSON map containing the route and recommended fuel
stops.

## Features

- Geocodes US start and finish locations and calculates a driving route.
- Finds trusted fuel stations within five miles of the route using PostGIS.
- Supports routes that require multiple refueling stops.
- Prioritizes route feasibility and the minimum number of stops, then fuel
  price.
- Calculates gallons purchased, cost at each stop, and total trip fuel cost.
- Returns the complete route and stop markers as GeoJSON.
- Provides an interactive Leaflet demo at `/demo/`.
- Caches geocoding and routing responses in Redis.
- Restores pre-geocoded station data without repeating the external backfill.
- Handles routing-provider failures with structured API errors.

## Technology stack

- Python 3.13
- Django 6.1
- PostgreSQL 17 with PostGIS 3.5
- Redis 8
- openrouteservice for endpoint geocoding and driving directions
- Geoapify for the station-geocoding workflow and demo map tiles
- Leaflet for the interactive map
- Docker Compose for local development

## Architecture

```text
Client / demo page
        |
        v
Django fuel-route endpoint
        |
        +----> Redis cache ----> openrouteservice on a cache miss
        |
        +----> PostGIS five-mile route-corridor query
        |
        v
Fuel-stop optimizer
        |
        v
JSON response + GeoJSON FeatureCollection
```

The API normally makes three openrouteservice requests for a previously
unseen trip: two geocoding requests and one directions request. Repeated
locations and routes are served from Redis and require no additional routing
provider calls.

Redis is an optimization rather than a hard dependency at request time. If a
cache operation fails, the application logs the failure and continues by
calling the routing provider.

### Request lifecycle

1. Validate and normalize the `start` and `finish` strings.
2. Geocode both locations with results restricted to the United States.
3. Request one complete driving route from openrouteservice.
4. Build the route line and query PostGIS for trusted stations within five
   miles.
5. Calculate each station's position along the route and distance from it.
6. Select a feasible, cost-conscious set of stops.
7. Calculate fuel purchases and costs.
8. Return trip data and a GeoJSON map in one response.

## Fuel-stop optimization

The optimizer uses the following fixed vehicle assumptions:

| Setting | Value |
| --- | ---: |
| Maximum range | 500 miles |
| Fuel efficiency | 10 MPG |
| Tank capacity | 50 gallons |
| Starting tank | Full |
| Station corridor | 5 miles from the route |

Station selection happens in two stages:

1. A backwards dynamic-programming pass determines the minimum number of
   reachable stops required to complete the route. A route is rejected if any
   required leg exceeds 500 miles.
2. Among stations that preserve that minimum-stop plan, the optimizer prefers
   the lowest price. Distance from the route, progress along the route, and
   OPIS ID provide deterministic tie-breaking.

At a selected stop, the vehicle buys only enough fuel to reach a cheaper
selected station when one is reachable. Otherwise, it buys enough to reach
the destination or fills up to the 50-gallon tank capacity. The reported
`total_cost` covers fuel purchased during the trip; it does not assign a cost
to the fuel already present in the full starting tank.

Station detour mileage is intentionally excluded. Route distance, duration,
fuel consumption, and fuel cost are calculated from the original route;
driving from that route to a selected station and back is not added. This
trade-off avoids additional directions requests and satisfies the preference
for a single routing call.

## Database design

The application uses one domain table, `FuelStation`, in addition to Django's
framework tables. Each row represents one station identified by its OPIS
truck-stop ID.

Important fields include:

- Station identity and address data
- Lowest retail price supplied for that OPIS ID
- Number of source price records represented by the stored price
- A PostGIS `Point` in WGS 84 (`SRID 4326`)
- Geocoding provider, status, confidence, matched address, and timestamps

The price import retains the lowest supplied price for duplicate OPIS IDs.
The source data does not identify a fuel grade or fuel type, so `Retail Price`
is treated as one generic per-gallon fuel price.

Only stations with `exact`, `high_confidence`, or `approximate` geocoding
status are eligible for route planning. City-centroid and unmatched records
are deliberately excluded to avoid displaying misleading stops far from the
actual route.

## Project structure

```text
.
├── config/                         Django settings and root URLs
├── data/
│   ├── processed/                  Reproducible station-location seed
│   ├── raw/                        Assessment fuel-price CSV
│   └── reports/                    Geocoding review reports
├── routing/
│   ├── management/commands/        Data import/export and geocoding commands
│   ├── migrations/                 Database schema migrations
│   ├── services/                   Routing, station search, and optimization
│   ├── static/routing/             Demo CSS and JavaScript
│   ├── templates/routing/          Interactive demo template
│   ├── tests/                      Automated test suite
│   ├── models.py                   FuelStation model
│   ├── urls.py                     API routes
│   └── views.py                    API and demo views
├── compose.yml                     Local service orchestration
├── Dockerfile                      Application image
└── requirements.txt                Pinned Python dependencies
```

## Local setup

### Prerequisites

- Docker Desktop or Docker Engine
- Docker Compose
- An openrouteservice API key
- A Geoapify API key for the interactive map

The committed location seed means that Geoapify is not called during normal
database setup.

### 1. Configure the environment

Copy the example file:

```bash
cp .env.example .env
```

Replace every `replace-me` value in `.env`. A development Django secret can
be generated with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django cryptographic signing key |
| `DJANGO_DEBUG` | Enables development error pages |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated accepted hostnames |
| `POSTGRES_*` | PostgreSQL connection settings |
| `REDIS_URL` | Redis cache connection URL |
| `GEOAPIFY_API_KEY` | Geoapify tiles in the browser demo |
| `ORS_API_KEY` | Server-side geocoding and routing |

Optional settings have safe application defaults:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `HTTP_TIMEOUT_SECONDS` | `30` | External request timeout |
| `ORS_GEOCODING_CACHE_TIMEOUT` | `2592000` | Geocode cache TTL in seconds |
| `ORS_ROUTE_CACHE_TIMEOUT` | `86400` | Route cache TTL in seconds |

### 2. Start the services

```bash
docker compose up --build -d
```

This starts Django, PostgreSQL/PostGIS, and Redis. The Compose development
service runs Django's development server; the image itself defaults to
Gunicorn when it is run without the Compose command override.

### 3. Create the database schema

```bash
docker compose exec web python manage.py migrate
```

### 4. Load the station data

Import the supplied fuel prices first, then restore trusted coordinates:

```bash
docker compose exec web python manage.py import_fuel_prices \
  --file data/raw/fuel-prices.csv

docker compose exec web python manage.py import_station_locations \
  --file data/processed/station-locations.csv
```

The location seed contains 3,150 trusted station locations. Its import is
idempotent: matching records remain untouched, and the command validates all
rows before applying updates. Restoring it makes a fresh environment usable
without spending Geoapify credits on the original backfill.

### 5. Verify the installation

```bash
docker compose exec web python manage.py check
docker compose exec web python manage.py test
```

The API is now available at <http://localhost:8000/api/v1/fuel-route/> and the
interactive demo at <http://localhost:8000/demo/>.

## API

### Calculate a fuel route

```http
POST /api/v1/fuel-route/
Content-Type: application/json
```

No bearer token or application-level authentication is required for this
assessment API.

Request body:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `start` | string | Yes | Starting location within the USA |
| `finish` | string | Yes | Destination within the USA |

Both fields are normalized for repeated whitespace and may contain at most
255 characters.

Example request:

```bash
curl --request POST http://localhost:8000/api/v1/fuel-route/ \
  --header 'Content-Type: application/json' \
  --header 'Accept-Encoding: gzip' \
  --data '{
    "start": "New York, NY",
    "finish": "Los Angeles, CA"
  }'
```

### Successful response

The endpoint returns HTTP `200` with five top-level values:

| Field | Description |
| --- | --- |
| `start` | Normalized query, matched label, longitude, and latitude |
| `finish` | Normalized query, matched label, longitude, and latitude |
| `route` | Distance, duration, bounding box, and provider attribution |
| `fuel_plan` | Vehicle assumptions, totals, and ordered fuel stops |
| `map` | GeoJSON `FeatureCollection` for direct map rendering |

Each fuel stop contains:

- Sequence number and OPIS ID
- Station name and address
- Longitude and latitude
- Miles along the route and miles from the route
- Price per gallon
- Arrival fuel, gallons purchased, and departure fuel
- Cost for that stop

The `map.features` array is ordered as follows:

1. Complete route `LineString`
2. Starting-location `Point`
3. Zero or more fuel-stop `Point` features
4. Destination `Point`

Monetary values are returned as two-decimal strings to preserve formatting.
The response supports gzip compression when the client sends an appropriate
`Accept-Encoding` header.

### Error responses

Errors use a consistent structure:

```json
{
  "error": {
    "code": "location_not_found",
    "message": "The start location could not be found within the USA."
  }
}
```

| HTTP status | Example reason |
| ---: | --- |
| `400` | Invalid input or start and finish resolve to the same location |
| `404` | Start or finish could not be geocoded in the USA |
| `405` | Method other than `POST` |
| `415` | Content type is not `application/json` |
| `422` | No driving route or feasible 500-mile fuel plan |
| `502` | Invalid response from the routing provider |
| `503` | Routing authentication, quota, or availability problem |
| `504` | Routing provider timed out |

## Interactive demo

Open <http://localhost:8000/demo/>, enter two US locations, and select
**Calculate route**. The page calls the same public API endpoint and displays:

- Route distance and estimated driving time
- Total fuel cost and number of stops
- An ordered stop summary
- The route, start, finish, and numbered fuel stops on a Leaflet map

The demo uses Geoapify map tiles. A visible tile error normally indicates a
missing, invalid, or restricted `GEOAPIFY_API_KEY` rather than a routing
failure.

## Data-management commands

### Refresh the reproducible location seed

After reviewing or improving geocoding data, export the trusted locations:

```bash
docker compose exec web python manage.py export_station_locations \
  --output data/processed/station-locations.csv
```

### Export stations requiring review

```bash
docker compose exec web python manage.py export_unmatched_stations \
  --output data/reports/unmatched-stations.csv
```

### Geocoding backfill

The `geocode_fuel_stations` command supports asynchronous Geoapify batch jobs,
result retrieval, city fallbacks, and proximity retries. The committed seed is
the recommended setup path; the backfill is only needed when rebuilding or
improving the coordinate dataset.

View the command's current options before running a paid or quota-consuming
operation:

```bash
docker compose exec web python manage.py help geocode_fuel_stations
```

## Tests

Run the complete suite:

```bash
docker compose exec web python manage.py test
```

Run an individual module during development:

```bash
docker compose exec web python manage.py test \
  routing.tests.test_fuel_optimizer
```

The test suite covers CSV import and export, geocoding workflows, provider
clients, Redis caching and failure fallback, spatial station search, fuel
optimization, API validation and serialization, gzip responses, and the demo
page.

## Assumptions and trade-offs

- Both requested locations must resolve within the United States.
- The vehicle starts with 50 gallons. Initial fuel is not included in trip
  spending.
- Range and MPG are fixed rather than request parameters.
- Fuel prices are treated as static assessment data.
- The supplied CSV has no explicit fuel grade/type, so one generic price is
  used per station.
- Only trusted coordinates are considered; this intentionally sacrifices
  coverage to avoid inaccurate map markers.
- Stations are searched within five miles of the route, but the route itself
  is not recalculated through each station. Station detour mileage, time, and
  fuel are intentionally excluded to keep routing-provider usage to one
  directions call.
- Optimization first minimizes the number of required stops and then prefers
  cheaper feasible stations. It is not a global optimization of fuel cost,
  driving detours, traffic, taxes, or real-time price changes.
- The current calculation permits fractional purchases below one gallon.
- No application authentication or rate limiting is included because this is
  a self-contained assessment API. Those controls should be added before a
  public production deployment.
- The Geoapify tile key is delivered to the browser for map rendering and
  should be restricted by allowed origin in the provider dashboard.

## Production considerations

Before deploying publicly:

- Set `DJANGO_DEBUG=false` and use a strong, secret signing key.
- Restrict `DJANGO_ALLOWED_HOSTS` and the Geoapify browser key.
- Put the application behind HTTPS and a reverse proxy.
- Add authentication or per-client rate limiting.
- Serve collected static assets from an appropriate static-file service.
- Use a managed PostGIS database and Redis instance with backups and
  monitoring.
- Add structured application monitoring and external-provider metrics.
