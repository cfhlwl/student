from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("collector", "0012_uploadsession_document_manifest_and_more")]

    operations = [
        migrations.AlterField(
            model_name="photosupplement",
            name="status",
            field=models.CharField(choices=[("pending", "待管理员确认"), ("approved", "已采用"), ("rejected", "已退回"), ("expired", "原提交已失效")], default="pending", max_length=12),
        ),
        migrations.AddConstraint(
            model_name="supplementphoto",
            constraint=models.UniqueConstraint(condition=Q(("student__isnull", False)), fields=("supplement", "student"), name="unique_supplement_student_binding"),
        ),
    ]
