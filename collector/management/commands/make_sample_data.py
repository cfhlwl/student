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
        headers = ["学生编号", "姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "照片文件名", "备注"]
        sheet.merge_cells("A1:J1"); sheet["A1"] = "填写说明：一行一位学生；姓名、学部、学年、年级、班级、就读类型为必填。"
        sheet.merge_cells("A2:J2"); sheet["A2"] = "就读类型只能填写：走读、非走读、半走读；性别只能填写：男、女；照片文件名建议填写：姓名.jpg。"
        for cell_ref in ("A1", "A2"):
            sheet[cell_ref].font = Font(bold=True, color="FFFFFF" if cell_ref == "A1" else "17233B")
            sheet[cell_ref].fill = PatternFill("solid", fgColor="17233B" if cell_ref == "A1" else "E8F5F2")
            sheet[cell_ref].alignment = Alignment(vertical="center")
        for col, header in enumerate(headers, start=1):
            cell = sheet.cell(row=4, column=col, value=header)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="E76F3C")
        rows = [["S-001", "示例张", "男", "高中部", "2026-2027", "高一", "1", "走读", "示例张.jpg", "虚构数据"], ["S-002", "示例李", "女", "高中部", "2026-2027", "高一", "1", "半走读", "示例李.jpg", "虚构数据"], ["S-003", "示例王", "男", "高中部", "2026-2027", "高一", "1", "非走读", "", "演示缺照片"]]
        for row_number, row in enumerate(rows, start=5):
            for col, value in enumerate(row, start=1): sheet.cell(row=row_number, column=col, value=value)
        sheet.freeze_panes = "A5"
        book.save(target / "示例学生名单.xlsx")
        for name, color in [("示例张.jpg", (35, 92, 130)), ("示例李.jpg", (183, 88, 60))]:
            image = Image.new("RGB", (240, 300), color); draw = ImageDraw.Draw(image); draw.text((75, 140), name.split(".")[0], fill="white"); image.save(target / name, quality=90)
        self.stdout.write(self.style.SUCCESS(f"示例数据已生成到 {target.resolve()}"))

