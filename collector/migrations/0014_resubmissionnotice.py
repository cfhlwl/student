from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("collector", "0013_photosupplement_expired_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ResubmissionNotice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reason_code", models.CharField(choices=[("photo_unclear", "照片不清晰"), ("face_too_small", "人脸尺寸过小"), ("exposure", "照片亮度或曝光不合格"), ("obstruction", "面部存在遮挡或明显侧脸"), ("photo_mismatch", "照片与学生不匹配"), ("missing_photo", "缺少照片"), ("custom", "自定义原因")], max_length=32)),
                ("reason", models.CharField(max_length=200)),
                ("sent_at", models.DateTimeField(auto_now=True)),
                ("is_active", models.BooleanField(default=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("sent_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="sent_resubmission_notices", to=settings.AUTH_USER_MODEL)),
                ("submission", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="resubmission_notice", to="collector.submission")),
            ],
            options={
                "ordering": ["-sent_at"],
                "indexes": [models.Index(fields=["is_active", "-sent_at"], name="notice_active_sent_idx")],
            },
        ),
    ]
