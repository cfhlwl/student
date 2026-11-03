from getpass import getpass

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "创建或更新独立管理员账号，不接受默认弱密码。"

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        username = options["username"].strip()
        if not username:
            raise CommandError("用户名不能为空")
        password = getpass("管理员密码（至少 12 位）：")
        confirm = getpass("再次输入密码：")
        if password != confirm:
            raise CommandError("两次密码不一致")
        if len(password) < 12:
            raise CommandError("管理员密码至少 12 位")
        user, _ = User.objects.get_or_create(username=username, defaults={"is_staff": True, "is_active": True})
        user.is_staff = True
        user.is_active = True
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(f"管理员 {username} 已安全创建/更新。"))

