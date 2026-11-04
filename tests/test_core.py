import io
import zipfile

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook, load_workbook
from PIL import Image

from collector.models import AppSetting, AuditLog, ClassGroup, NamingRule, SchoolYear, Student, StudentPhoto, Submission
from collector.services import ValidationError, clear_all_collection_data, clear_rejected_submissions, create_submission, export_students, parse_roster, template_bytes


def excel_upload(rows):
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    for row in rows:
        if len(row) == 10:  # keep older test fixtures readable while the public contract stays at 7 columns
            row = [row[1], row[2], row[3], str(row[4]).split("-")[0], row[6], row[7], row[9]]
        sheet.append(row)
    data = io.BytesIO(); book.save(data)
    return SimpleUploadedFile("名单.xlsx", data.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def image_upload(name, color=(220, 80, 60)):
    data = io.BytesIO()
    image = Image.new("RGB", (40, 40), color); image.save(data, format="JPEG")
    return SimpleUploadedFile(name, data.getvalue(), content_type="image/jpeg")


@pytest.mark.django_db
def test_submission_matches_photo_by_unique_name_and_keeps_token_isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    rows = [["张三", "男", "高中部", "2026", "1", "走读", ""]]
    submission, token = create_submission(roster_upload=excel_upload(rows), photo_uploads=[image_upload("张三.jpg")], document_uploads=[])
    assert len(token) >= 40
    assert submission.students.get().match_status == "matched"
    assert submission.students.get().photo.file.name.endswith("张三.jpg")
    assert submission.students.get().grade == "高一"
    assert submission.students.get().student_no == ""
    assert submission.students.get().photo_filename_hint == ""
    assert submission.roster_file.name.endswith("名单.xlsx")
    assert Submission.objects.filter(id=submission.id).exists()


@pytest.mark.django_db
def test_duplicate_excel_row_and_cross_class_are_rejected():
    duplicate = [["A-001", "张三", "男", "高中部", "2026-2027", "高一", "1", "走读", "", ""], ["A-001", "张三", "男", "高中部", "2026-2027", "高一", "1", "走读", "", ""]]
    with pytest.raises(ValidationError, match="Excel 存在逐行错误"):
        create_submission(roster_upload=excel_upload(duplicate), photo_uploads=[], document_uploads=[])
    cross_class = [["A-001", "张三", "男", "高中部", "2026-2027", "高一", "1", "走读", "", ""], ["A-002", "李四", "女", "高中部", "2026-2027", "高一", "2", "住宿", "", ""]]
    with pytest.raises(ValidationError, match="一次提交只能包含同一学部、学年、年级和班级"):
        create_submission(roster_upload=excel_upload(cross_class), photo_uploads=[], document_uploads=[])


@pytest.mark.django_db
def test_same_name_without_explicit_photo_is_not_randomly_matched(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    rows = [["A-001", "王芳", "女", "高中部", "2026-2027", "高一", "1", "走读", "", ""], ["A-002", "王芳", "女", "高中部", "2026-2027", "高一", "1", "住宿", "", ""]]
    submission, _ = create_submission(roster_upload=excel_upload(rows), photo_uploads=[image_upload("王芳.jpg")], document_uploads=[])
    assert list(submission.students.values_list("match_status", flat=True)) == ["ambiguous", "ambiguous"]


@pytest.mark.django_db
def test_export_contains_per_class_statistics_and_total(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    first_rows = [["张三", "男", "高中部", "2026", "1", "走读", ""]]
    second_rows = [["李四", "女", "高中部", "2026", "2", "半走读", ""]]
    first, _ = create_submission(roster_upload=excel_upload(first_rows), photo_uploads=[image_upload("张三.jpg")], document_uploads=[])
    second, _ = create_submission(roster_upload=excel_upload(second_rows), photo_uploads=[image_upload("李四.jpg")], document_uploads=[])
    User.objects.create_user(username="admin", password="a-strong-password-123", is_staff=True)
    first.status = "approved"; first.save(update_fields=["status"])
    second.status = "approved"; second.save(update_fields=["status"])
    payload, count, preview = export_students({"school_year__name": "2026", "department": "high"})
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert any(name.endswith("/学生统计表.xlsx") for name in names)
        assert "总统计表.xlsx" in names
        assert any(name.endswith("/走读/学生照片/张三.jpg") for name in names)
        assert any(name.endswith("/半走读/学生照片/李四.jpg") for name in names)
        assert any(name.endswith("/学生名单/名单.xlsx") for name in names)
        stats_file = next(name for name in names if name.endswith("/学生统计表.xlsx"))
        stats = load_workbook(io.BytesIO(archive.read(stats_file)), read_only=True)
        assert set(stats.sheetnames) == {"统计汇总", "学生明细", "异常记录"}
    assert count == 2
    assert preview == ["张三.jpg", "李四.jpg"]


@pytest.mark.django_db
def test_all_export_splits_study_types_and_includes_original_roster(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    rows = [["走读生", "男", "高中部", "2026", "1", "走读", ""], ["半走读生", "女", "高中部", "2026", "1", "半走读", ""], ["住宿生", "男", "高中部", "2026", "1", "住宿", ""]]
    submission, _ = create_submission(roster_upload=excel_upload(rows), photo_uploads=[image_upload("走读生.jpg"), image_upload("半走读生.jpg"), image_upload("住宿生.jpg")], document_uploads=[])
    submission.status = "approved"; submission.save(update_fields=["status"])
    payload, count, _ = export_students({"school_year__name": "2026", "department": "high"})
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert "高中部/高一/1/走读/学生统计表.xlsx" in names
        assert "高中部/高一/1/半走读/学生统计表.xlsx" in names
        assert "高中部/高一/1/住宿/学生统计表.xlsx" in names
        assert "高中部/高一/1/走读/学生照片/走读生.jpg" in names
        assert "高中部/高一/1/半走读/学生照片/半走读生.jpg" in names
        assert "高中部/高一/1/住宿/学生照片/住宿生.jpg" in names
        assert "高中部/高一/1/学生名单/名单.xlsx" in names
    assert count == 3


@pytest.mark.django_db
def test_public_result_requires_token_and_staff_boundary(client):
    rows = [["A-001", "张三", "男", "高中部", "2026-2027", "高一", "1", "走读", "", ""]]
    submission, token = create_submission(roster_upload=excel_upload(rows), photo_uploads=[], document_uploads=[])
    denied = client.get(reverse("submit-result", args=[submission.id]))
    assert denied.status_code == 404
    allowed = client.get(reverse("submit-result", args=[submission.id]) + f"?token={token}")
    assert allowed.status_code == 200
    dashboard = client.get(reverse("admin-dashboard"))
    assert dashboard.status_code == 302


@pytest.mark.django_db
def test_public_home_has_title_and_floating_photo_guide(client):
    response = client.get(reverse("submit"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "赤峰一中学生资料采集" in content
    assert 'class="public-title"' not in content
    assert "统一入口 / 2026 学年" not in content
    assert "当前公开上传" not in content
    assert 'class="photo-guide-float"' in content
    assert 'data-photo="/static/images/photo-guide-girl.png"' in content
    admin = User.objects.create_user(username="public-home-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    staff_content = client.get(reverse("submit")).content.decode()
    assert "管理工作台" not in staff_content
    assert "退出" not in staff_content


@pytest.mark.django_db
def test_export_zip_without_filters_returns_zip(client):
    admin = User.objects.create_user(username="export-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    response = client.get(reverse("export-zip"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    assert b"".join(response.streaming_content)[:2] == b"PK"
    response.close()


@pytest.mark.django_db
def test_upload_gate_closes_public_entry(client):
    AppSetting.set_value("public_upload_open", False)
    response = client.get(reverse("submit"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_dashboard_always_shows_clear_rejected_data_button(client):
    admin = User.objects.create_user(username="dashboard-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    response = client.get(reverse("admin-dashboard"))
    assert response.status_code == 200
    assert reverse("clear-test-data") in response.content.decode()
    assert "清空全部采集数据" in response.content.decode()


@pytest.mark.django_db
def test_clear_all_collection_data_resets_database_but_keeps_admin(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    rows = [["A-001", "张三", "男", "高中部", "2026-2027", "高一", "1", "走读", "A-001.jpg", ""]]
    submission, _ = create_submission(roster_upload=excel_upload(rows), photo_uploads=[image_upload("A-001.jpg")], document_uploads=[])
    submission.status = "approved"
    submission.save(update_fields=["status"])
    admin = User.objects.create_user(username="reset-admin", password="strong-password-123", is_staff=True)
    NamingRule.current()
    AppSetting.set_value("public_upload_open", False)
    counts = clear_all_collection_data()
    assert counts["submissions"] == 1
    assert counts["students"] == 1
    assert counts["photos"] == 1
    assert counts["rosters"] == 1
    assert Submission.objects.count() == 0
    assert Student.objects.count() == 0
    assert StudentPhoto.objects.count() == 0
    assert SchoolYear.objects.count() == 0
    assert ClassGroup.objects.count() == 0
    assert NamingRule.objects.count() == 0
    assert AppSetting.objects.count() == 0
    assert AuditLog.objects.count() == 0
    assert User.objects.filter(username=admin.username).exists()
    assert list((tmp_path / "submissions").iterdir()) == []


@pytest.mark.django_db
def test_configured_collection_scope_rejects_unlisted_class():
    AppSetting.set_value("collection_scope", "configured")
    rows = [["A-001", "张三", "男", "高中部", "2026-2027", "高一", "99", "走读", "", ""]]
    try:
        with pytest.raises(ValidationError, match="当前仅允许已配置班级提交"):
            create_submission(roster_upload=excel_upload(rows), photo_uploads=[], document_uploads=[])
    finally:
        AppSetting.set_value("collection_scope", "all")


@pytest.mark.django_db
def test_clear_rejected_submissions_removes_files_but_keeps_approved(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    rows = [["A-001", "张三", "男", "高中部", "2026-2027", "高一", "1", "走读", "A-001.jpg", ""]]
    rejected, _ = create_submission(roster_upload=excel_upload(rows), photo_uploads=[image_upload("A-001.jpg")], document_uploads=[])
    rejected.status = "rejected"; rejected.save(update_fields=["status"])
    approved, _ = create_submission(roster_upload=excel_upload(rows), photo_uploads=[], document_uploads=[])
    approved.status = "approved"; approved.save(update_fields=["status"])
    result = clear_rejected_submissions()
    assert result[0:2] == (1, 1)
    assert Submission.objects.filter(id=rejected.id).exists() is False
    assert Submission.objects.filter(id=approved.id).exists() is True
    assert StudentPhoto.objects.filter(submission=rejected.id).exists() is False


@pytest.mark.django_db
def test_manual_match_binds_only_same_submission(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    rows = [["A-001", "王芳", "女", "高中部", "2026-2027", "高一", "1", "走读", "", ""], ["A-002", "王芳", "女", "高中部", "2026-2027", "高一", "1", "住宿", "", ""]]
    submission, _ = create_submission(roster_upload=excel_upload(rows), photo_uploads=[image_upload("王芳.jpg")], document_uploads=[])
    admin = User.objects.create_user(username="manual-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    photo = StudentPhoto.objects.get(submission=submission, student__isnull=True)
    student = submission.students.first()
    response = client.post(reverse("manual-match", args=[photo.id]), {"student_id": student.id})
    assert response.status_code == 302
    photo.refresh_from_db(); student.refresh_from_db()
    assert photo.student_id == student.id
    assert student.match_status == "matched"


@pytest.mark.django_db
def test_export_escapes_formula_like_user_values(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    rows = [["正常姓名", "男", "高中部", "2026", "1", "走读", ""]]
    submission, _ = create_submission(roster_upload=excel_upload(rows), photo_uploads=[], document_uploads=[])
    student = submission.students.get()
    student.name = "=姓名"; student.save(update_fields=["name"])
    submission.status = "approved"; submission.save(update_fields=["status"])
    payload, _, _ = export_students({})
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        stats_file = next(name for name in archive.namelist() if name.endswith("/学生统计表.xlsx"))
        book = load_workbook(io.BytesIO(archive.read(stats_file)), data_only=False)
        assert book["学生明细"]["A2"].value == "'=姓名"


def test_template_has_expected_columns():
    book = load_workbook(io.BytesIO(template_bytes()), read_only=True)
    rows = list(book.active.values)
    assert rows[3][:8] == ("姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "备注")
    assert rows[4][:7] == ("示例同学", "男", "高中部", "2026", "高一", "1", "走读")


def test_parser_accepts_template_instruction_rows():
    upload = SimpleUploadedFile("模板.xlsx", template_bytes(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert parse_roster(upload)[0]["姓名"] == "示例同学"
    assert parse_roster(upload)[0]["年级"] == "高一"


def test_parser_derives_middle_school_grade_from_school_year():
    upload = excel_upload([["初中同学", "女", "初中部", "2024", "1", "住宿", ""]])
    assert parse_roster(upload)[0]["年级"] == "初三"

