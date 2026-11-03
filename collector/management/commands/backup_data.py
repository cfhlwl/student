import shutil
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "备份 SQLite 数据库与私有文件，输出为一个可恢复的 zip。"

    def add_arguments(self, parser):
        parser.add_argument("--output", default="backups", help="备份目录")

    def handle(self, *args, **options):
        target_dir = Path(options["output"]).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = target_dir / f"student-collector-{stamp}.zip"
        database = Path(settings.DATABASES["default"]["NAME"])
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            if database.exists(): archive.write(database, "db.sqlite3")
            if settings.MEDIA_ROOT.exists():
                for file in settings.MEDIA_ROOT.rglob("*"):
                    if file.is_file(): archive.write(file, Path("media") / file.relative_to(settings.MEDIA_ROOT))
        self.stdout.write(self.style.SUCCESS(f"备份已创建：{output}"))

