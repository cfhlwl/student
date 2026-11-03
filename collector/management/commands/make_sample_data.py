import io
from pathlib import Path

from django.core.management.base import BaseCommand
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image, ImageDraw


class Command(BaseCommand):
    help = "生成虚构学生的 Excel 示例名单与照片，供本地联验。"

    def add_arguments(self, parser):
        parser.add_argument("--output", default="sample_data")

    def handle(self, *args, **options):
        target = Path(options["output"]); target.mkdir(parents=True, exist_ok=True)
        book = Workbook(); sheet = book.active; sheet.title = "学生名单"
        headers = ["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "备注"]
        sheet.merge_cells("A1:H1"); sheet["A1"] = "填写说明：一行一位学生；姓名、学部、学年、年级、班级、就读类型为必填；学年是当前在读学年的起始年份，不是入学年份。"
        sheet.merge_cells("A2:H2"); sheet["A2"] = "同一学年可填写初一、初二、初三、高一、高二或高三；旧版没有年级列的名单仍按旧学年映射兼容；照片默认按姓名匹配。"
        for cell_ref in ("A1", "A2"):
            sheet[cell_ref].font = Font(bold=True, color="FFFFFF" if cell_ref == "A1" else "17233B")
            sheet[cell_ref].fill = PatternFill("solid", fgColor="17233B" if cell_ref == "A1" else "E8F5F2")
            sheet[cell_ref].alignment = Alignment(vertical="center")
        for col, header in enumerate(headers, start=1):
            cell = sheet.cell(row=4, column=col, value=header)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="E76F3C")
        rows = [["示例张", "男", "高中部", "2026", "高一", "1", "走读", "虚构数据"], ["示例李", "女", "高中部", "2026", "高一", "1", "半走读", "虚构数据"], ["示例王", "男", "高中部", "2026", "高一", "1", "住宿", "演示缺照片"]]
        for row_number, row in enumerate(rows, start=5):
            for col, value in enumerate(row, start=1): sheet.cell(row=row_number, column=col, value=value)
        sheet.freeze_panes = "A5"
        book.save(target / "示例学生名单.xlsx")
        for name, color in [("示例张.jpg", (35, 92, 130)), ("示例李.jpg", (183, 88, 60))]:
            image = Image.new("RGB", (240, 300), color); draw = ImageDraw.Draw(image); draw.text((75, 140), name.split(".")[0], fill="white"); image.save(target / name, quality=90)
        self.stdout.write(self.style.SUCCESS(f"示例数据已生成到 {target.resolve()}"))

