from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("collector", "0017_exportjob_download_center"),
    ]

    operations = [
        migrations.AddField(
            model_name="exportjob",
            name="exported_photo_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exportjob",
            name="fallback_original_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exportjob",
            name="source_unavailable_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exportjob",
            name="rejection_reasons",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
