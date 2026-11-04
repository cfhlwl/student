from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("collector", "0011_uploadsession_start_idempotency_key")]

    operations = [
        migrations.AddField(
            model_name="uploadsession",
            name="document_manifest",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="uploadsession",
            name="document_manifest_digest",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
