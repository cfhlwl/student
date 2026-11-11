import io

from openpyxl import Workbook
from PIL import Image

from collector.preflight import PreflightLimits, run_preflight


def roster_file(headers, rows, name="名单.xlsx"):
    book = Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    data = io.BytesIO()
    book.save(data)
    data.seek(0)
    data.name = name
    return data


def photo_file(name, *, color=(220, 80, 60), broken=False):
    data = io.BytesIO()
    if broken:
        data.write(b"not-an-image")
    else:
        Image.new("RGB", (40, 40), color).save(data, format="JPEG")
    data.seek(0)
    data.name = name
    data.size = len(data.getvalue())
    return data


def test_preflight_reports_match_missing_and_same_name_manual_warning():
    roster = roster_file(
        ["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型"],
        [
            ["张三", "男", "高中部", "2026", "高一", "1", "走读"],
            ["王芳", "女", "高中部", "2026", "高一", "1", "住宿"],
            ["王芳", "女", "高中部", "2026", "高一", "1", "半走读"],
        ],
    )
    result = run_preflight(roster, [photo_file("张三.jpg"), photo_file("陌生人.jpg")])

    assert result.summary == {
        "student_count": 3,
        "photo_count": 2,
        "matched_count": 1,
        "missing_count": 2,
        "manual_count": 1,
    }
    assert not result.blocking_errors
    assert {issue.code for issue in result.warnings} == {"same_name_student", "unmatched_photo", "no_face"}
    assert not result.can_continue()
    assert result.can_continue(warnings_acknowledged=True)


def test_preflight_blocks_row_errors_mixed_scope_and_duplicate_rows():
    roster = roster_file(
        ["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型"],
        [
            ["重复生", "男", "高中部", "2026", "高一", "1", "走读"],
            ["重复生", "男", "高中部", "2026", "高一", "1", "走读"],
            ["另一个班", "女", "高中部", "2026", "高一", "2", "住宿"],
            ["错年级", "女", "高中部", "2026", "初一", "1", "住宿"],
        ],
    )
    result = run_preflight(roster, [])
    codes = {issue.code for issue in result.blocking_errors}

    assert "duplicate_student_row" in codes
    assert "invalid_grade" in codes
    assert "mixed_class_scope" in codes
    assert not result.can_continue(warnings_acknowledged=True)


def test_preflight_blocks_photo_type_size_total_and_duplicate_filename():
    roster = roster_file(
        ["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型"],
        [["张三", "男", "高中部", "2026", "高一", "1", "走读"]],
    )
    limits = PreflightLimits(max_photo_count=3, max_photo_bytes=5, max_total_photo_bytes=8)
    result = run_preflight(
        roster,
        [photo_file("张三.jpg"), photo_file("张三.JPG"), photo_file("张三.txt", broken=True)],
        limits=limits,
    )
    codes = {issue.code for issue in result.blocking_errors}

    assert "duplicate_photo_filename" in codes
    assert "photo_too_large" in codes
    assert "unsupported_photo_type" in codes
    assert "photo_total_too_large" in codes
    assert "invalid_photo" in codes


def test_preflight_accepts_legacy_grade_omission_and_reports_filename_collision():
    roster = roster_file(
        ["姓名", "性别", "学部", "学年", "班级", "就读类型"],
        [["李四", "女", "高中部", "2026", "3", "走读"]],
    )
    result = run_preflight(roster, [photo_file("李四.jpg"), photo_file("李四.jpg")])

    assert result.records[0].grade == "高一"
    assert "duplicate_photo_filename" in {issue.code for issue in result.blocking_errors}


def test_preflight_malformed_excel_is_a_blocking_error():
    broken = io.BytesIO(b"not-xlsx")
    broken.name = "名单.xlsx"
    result = run_preflight(broken, [])

    assert result.summary["student_count"] == 0
    assert "invalid_roster" in {issue.code for issue in result.blocking_errors}

