from django.contrib.gis.db import models as gis_models
from django.db import models


class FuelStation(models.Model):
    class GeocodingStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        EXACT = "exact", "Exact"
        HIGH_CONFIDENCE = "high_confidence", "High confidence"
        APPROXIMATE = "approximate", "Approximate"
        CITY_CENTROID = "city_centroid", "City centroid"
        UNMATCHED = "unmatched", "Unmatched"

    opis_id = models.PositiveBigIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    normalized_address = models.CharField(
        max_length=512,
        db_index=True,
    )
    city = models.CharField(max_length=120)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField()

    retail_price = models.DecimalField(
        max_digits=10,
        decimal_places=8,
    )
    source_price_count = models.PositiveIntegerField(default=1)

    location = gis_models.PointField(
        geography=True,
        srid=4326,
        spatial_index=True,
        null=True,
        blank=True,
    )

    geocoding_provider = models.CharField(
        max_length=50,
        blank=True,
    )
    geocoding_status = models.CharField(
        max_length=20,
        choices=GeocodingStatus,
        default=GeocodingStatus.PENDING,
        db_index=True,
    )
    geocoding_confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
    )
    matched_address = models.CharField(
        max_length=512,
        blank=True,
    )
    geocoded_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("opis_id",)
        indexes = [
            models.Index(
                fields=("state", "city"),
                name="station_state_city_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(retail_price__gt=0),
                name="station_price_gt_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(source_price_count__gt=0),
                name="station_price_count_gt_zero",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(geocoding_confidence__isnull=True)
                    | models.Q(
                        geocoding_confidence__gte=0,
                        geocoding_confidence__lte=1,
                    )
                ),
                name="station_confidence_valid",
            ),
        ]

    def __str__(self):
        return f"{self.opis_id} - {self.name}"
