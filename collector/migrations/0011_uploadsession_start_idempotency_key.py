from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("collector", "0010_alter_supplementphoto_size")]

    operations = [
        migrations.AddField(
            model_name="uploadsession",
            name="start_idempotency_key",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
    ]
