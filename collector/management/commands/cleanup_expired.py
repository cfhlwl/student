from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from collector.models import Submission


class Command(BaseCommand):
    help = "清理超过保留期且已退回的提交及其私有文件；已采用数据不会被默认清理。"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.DATA_RETENTION_DAYS)
        parser.add_argument("--yes", action="store_true", help="确认执行删除")

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        submissions = list(Submission.objects.filter(status="rejected", submitted_at__lt=cutoff).prefetch_related("photos", "documents"))
        if not options["yes"]:
            self.stdout.write(f"发现 {len(submissions)} 个超过 {options['days']} 天的已退回提交。加 --yes 执行清理。")
            return
        count = 0
        for submission in submissions:
            for obj in list(submission.photos.all()) + list(submission.documents.all()):
                obj.file.delete(save=False)
            submission.delete(); count += 1
        self.stdout.write(self.style.SUCCESS(f"已清理 {count} 个已退回提交。"))

