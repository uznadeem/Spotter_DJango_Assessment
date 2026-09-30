from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("routing", "0003_fuelstation_geocoding_job_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelstation",
            name="station_retry_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
