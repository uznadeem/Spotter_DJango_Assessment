from django.urls import path

from routing.views import fuel_route


app_name = "routing"

urlpatterns = [
    path(
        "fuel-route/",
        fuel_route,
        name="fuel-route",
    ),
]
