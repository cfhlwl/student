from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.core.validators
import uuid
import collector.models


class Migration(migrations.Migration):
    dependencies = [
        ("collector", "0008_uploadsession_expected_student_count"),
    ]

    operations = [
        migrations.CreateModel(
            name="PhotoSupplement",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("pending", "待管理员确认"), ("approved", "已采用"), ("rejected", "已退回")], default="pending", max_length=12)),
                ("note", models.CharField(blank=True, max_length=500)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_photo_supplements", to=settings.AUTH_USER_MODEL)),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="photo_supplements", to="collector.submission")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="SupplementPhoto",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_name", models.CharField(max_length=255)),
                ("file", models.FileField(upload_to=collector.models.supplement_photo_path)),
                ("content_type", models.CharField(max_length=100)),
                ("size", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("sha256", models.CharField(max_length=64)),
                ("match_status", models.CharField(choices=[("matched", "已匹配"), ("ambiguous", "待人工确认")], default="ambiguous", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("student", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="supplement_photos", to="collector.student")),
                ("supplement", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="photos", to="collector.photosupplement")),
            ],
        ),
        migrations.AddConstraint(
            model_name="photosupplement",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "pending")), fields=("submission",), name="unique_pending_photo_supplement_per_submission"),
        ),
    ]
