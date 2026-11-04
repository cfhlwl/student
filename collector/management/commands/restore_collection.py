from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from collector.backup import BackupError, restore_backup, verify_backup


class Command(BaseCommand):
    help = "校验并恢复 SQLite 与私有媒体配套备份；生产操作前必须停止写入。"

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="备份 ZIP 路径")
        parser.add_argument("--yes", action="store_true", help="确认覆盖目标数据库和媒体目录")

    def handle(self, *args, **options):
        source = Path(options["input"]).resolve()
        if not options["yes"]:
            raise CommandError("恢复会覆盖数据库和私有媒体，请确认后添加 --yes。")
        try:
            verify_backup(source)
            manifest = restore_backup(source)
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"恢复完成并通过 SHA256 校验：媒体文件 {len(manifest.get('media', []))} 个。"))
