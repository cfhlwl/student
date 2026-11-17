from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('collector', '0015_exportjob'),
    ]

    operations = [
        migrations.AddField(
            model_name='exportjob',
            name='attempt',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='exportjob',
            name='data_snapshot',
            field=models.CharField(default='', max_length=64),
        ),
        migrations.AddField(
            model_name='exportjob',
            name='lease_token',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AlterField(
            model_name='exportjob',
            name='status',
            field=models.CharField(choices=[('queued', '排队中'), ('running', '生成中'), ('completed', '已完成'), ('failed', '失败'), ('expired', '已过期'), ('stale', '已失效')], default='queued', max_length=12),
        ),
    ]
