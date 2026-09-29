from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = (
        "opis_id",
        "name",
        "city",
        "state",
        "retail_price",
        "geocoding_status",
    )
    list_filter = (
        "state",
        "geocoding_status",
    )
    search_fields = (
        "opis_id",
        "name",
        "address",
        "city",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "geocoded_at",
    )
    ordering = ("opis_id",)
    list_per_page = 50
