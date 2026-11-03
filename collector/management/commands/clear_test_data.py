from django.core.management.base import BaseCommand

from collector.services import clear_rejected_submissions


class Command(BaseCommand):
    help = "清除已退回/测试提交及其私有文件，不删除已采用数据。"

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="确认执行清除")

    def handle(self, *args, **options):
        if not options["yes"]:
            self.stdout.write("此命令只清除 status=rejected 的提交。确认后加 --yes 执行。")
            return
        submissions, students, files = clear_rejected_submissions()
        self.stdout.write(self.style.SUCCESS(f"已清除提交 {submissions} 个、学生 {students} 条、文件 {files} 个。"))
