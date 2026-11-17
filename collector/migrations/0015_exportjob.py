import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('collector', '0014_resubmissionnotice'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ExportJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fingerprint', models.CharField(max_length=64, unique=True)),
                ('filters', models.JSONField(default=dict)),
                ('status', models.CharField(choices=[('queued', '排队中'), ('running', '生成中'), ('completed', '已完成'), ('failed', '失败'), ('expired', '已过期')], default='queued', max_length=12)),
                ('requested_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('file_path', models.CharField(blank=True, max_length=500)),
                ('file_name', models.CharField(default='学生资料导出.zip', max_length=255)),
                ('student_count', models.PositiveIntegerField(default=0)),
                ('preview', models.JSONField(blank=True, default=list)),
                ('error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='export_jobs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['status', 'created_at'], name='export_job_status_created_idx')],
            },
        ),
    ]
