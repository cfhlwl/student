import io
from pathlib import Path

from django.core.management.base import BaseCommand
from openpyxl import Workbook
from PIL import Image, ImageDraw


class Command(BaseCommand):
    help = "生成虚构学生的 Excel 示例名单与照片，供本地联验。"

    def add_arguments(self, parser):
        parser.add_argument("--output", default="sample_data")

    def handle(self, *args, **options):
        target = Path(options["output"]); target.mkdir(parents=True, exist_ok=True)
        book = Workbook(); sheet = book.active; sheet.title = "学生名单"
        headers = ["学生编号", "姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "照片文件名", "备注"]
        sheet.append(headers)
        rows = [["S-001", "示例张", "男", "高中部", "2026-2027", "高一", "1", "走读", "示例张.jpg", "虚构数据"], ["S-002", "示例李", "女", "高中部", "2026-2027", "高一", "1", "半走读", "示例李.jpg", "虚构数据"], ["S-003", "示例王", "男", "高中部", "2026-2027", "高一", "1", "非走读", "", "演示缺照片"]]
        for row in rows: sheet.append(row)
        book.save(target / "示例学生名单.xlsx")
        for name, color in [("示例张.jpg", (35, 92, 130)), ("示例李.jpg", (183, 88, 60))]:
            image = Image.new("RGB", (240, 300), color); draw = ImageDraw.Draw(image); draw.text((75, 140), name.split(".")[0], fill="white"); image.save(target / name, quality=90)
        self.stdout.write(self.style.SUCCESS(f"示例数据已生成到 {target.resolve()}"))

