from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("collector", "0007_uploadsession_photo_manifest_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="uploadsession",
            name="expected_student_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
