# Fuel Route API

A Django API that calculates a driving route and recommends cost-effective fuel stops using the supplied fuel-price dataset.

## Local setup

Requirements: Docker and Docker Compose.

1. Copy `.env.example` to `.env` and provide the Django, PostgreSQL,
   Geoapify, and openrouteservice values.
2. Start the services:

   ```bash
   docker compose up --build -d
   ```

3. Create the database schema:

   ```bash
   docker compose exec web python manage.py migrate
   ```

4. Import the supplied prices, then restore the trusted station
   coordinates:

   ```bash
   docker compose exec web python manage.py import_fuel_prices \
     --file data/raw/fuel-prices.csv
   docker compose exec web python manage.py import_station_locations \
     --file data/processed/station-locations.csv
   ```

The location seed contains only exact, high-confidence, and approximate
matches. Restoring it does not call Geoapify, so a fresh environment is ready
without repeating the original geocoding backfill.

## Use the application

Open the interactive route planner at <http://localhost:8000/demo/> or call
the API directly:

```bash
curl --request POST http://localhost:8000/api/v1/fuel-route/ \
  --header 'Content-Type: application/json' \
  --data '{
    "start": "New York, NY",
    "finish": "Los Angeles, CA"
  }'
```

The response includes the route, fuel-plan totals, recommended stations, and
a GeoJSON `FeatureCollection` under `map`.

## Refresh the location seed

After reviewing or improving geocoding results, regenerate the committed seed
from the database with:

```bash
docker compose exec web python manage.py export_station_locations \
  --output data/processed/station-locations.csv
```

The import is idempotent: rows that already match are left unchanged.

## Tests

```bash
docker compose exec web python manage.py test
```
