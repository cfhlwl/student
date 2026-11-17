from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("collector", "0016_exportjob_attempt_exportjob_data_snapshot_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="exportjob",
            name="fingerprint",
            field=models.CharField(max_length=64),
        ),
        migrations.AddField(
            model_name="exportjob",
            name="processed_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="exportjob",
            name="total_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddIndex(
            model_name="exportjob",
            index=models.Index(fields=["fingerprint", "status"], name="export_job_fp_status_idx"),
        ),
    ]
