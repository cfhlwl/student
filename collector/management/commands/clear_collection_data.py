from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from collector.backup import BackupError, clear_collection_safely


class Command(BaseCommand):
    help = "先备份，再一致性清空所有采集资料；保留管理员账号。"

    def add_arguments(self, parser):
        parser.add_argument("--backup", required=True, help="清空前备份 ZIP 路径")
        parser.add_argument("--yes", action="store_true", help="确认清空全部采集资料")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("此操作会清空所有提交、学生、照片、文档、配置和日志；请添加 --yes。")
        try:
            counts = clear_collection_safely(Path(options["backup"]).resolve())
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"已备份并清空采集资料：{counts}"))
