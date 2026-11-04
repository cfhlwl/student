from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from collector.backup import BackupError, create_backup, verify_backup


class Command(BaseCommand):
    help = "创建并校验 SQLite 与私有媒体配套备份。"

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="备份 ZIP 路径")

    def handle(self, *args, **options):
        output = Path(options["output"]).resolve()
        try:
            created = create_backup(output)
            manifest = verify_backup(created)
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"备份已创建并校验：{created}；媒体文件 {len(manifest.get('media', []))} 个；数据库 {settings.DATABASES['default']['NAME']}"))
