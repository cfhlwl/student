import io

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import load_workbook

from collector.models import AppSetting, ClassGroup, SchoolYear, Student, StudentPhoto, Submission, SubmissionDocument
from collector.statistics import build_admin_statistics_context, build_exception_workbook


def _photo_upload(name="学生.jpg", content=b"fake-photo"):
    return SimpleUploadedFile(name, content, content_type="image/jpeg")


def _scope(year, department="high", grade="高一", name="1"):
    year_obj = SchoolYear.objects.get_or_create(name=year)[0]
    return ClassGroup.objects.create(department=department, school_year=year_obj, grade=grade, name=name)


def _submission(class_group, status, names, *, study_type="走读"):
    submission = Submission.objects.create(
        access_token_hash=f"{status}-{class_group.pk}-{len(names)}-{Submission.objects.count()}",
        department=class_group.department,
        school_year=class_group.school_year,
        grade=class_group.grade,
        class_group=class_group,
        status=status,
        source_filename="名单.xlsx",
    )
    for index, name in enumerate(names):
        student = Student.objects.create(
            submission=submission,
            name=name,
            gender="男" if index % 2 == 0 else "女",
            department=class_group.department,
            school_year=class_group.school_year,
            grade=class_group.grade,
            class_name=class_group.name,
            study_type=study_type,
            match_status="missing",
        )
        if index == 0:
            StudentPhoto.objects.create(
                student=student,
                submission=submission,
                original_name=f"{name}.jpg",
                file=_photo_upload(f"{name}.jpg"),
                content_type="image/jpeg",
                size=10,
                sha256="a" * 64,
            )
            student.match_status = "matched"
            student.save(update_fields=["match_status"])
    return submission


@pytest.mark.django_db
def test_statistics_uses_only_current_approved_version_and_keeps_coverage_distinct():
    scope = _scope("2026", name="1")
    old = _submission(scope, "rejected", ["旧学生", "旧学生2"])
    current = _submission(scope, "approved", ["新学生", "新学生2", "新学生3"])
    _submission(_scope("2026", name="2"), "pending", ["待审学生"])

    context = build_admin_statistics_context()
    rows = {row["class_name"]: row for row in context["rows"]}
    assert rows["1"]["submission_id"] == str(current.id)
    assert rows["1"]["student_count"] == 3
    assert rows["1"]["matched_photo_count"] == 1
    assert rows["1"]["missing_photo_count"] == 2
    assert rows["1"]["photo_completion_rate"] == pytest.approx(33.33)
    assert context["totals"]["student_count"] == 3
    assert context["coverage"]["available"] is False
    assert "全校应采" in context["coverage"]["reason"]
    assert old.id != current.id


@pytest.mark.django_db
def test_statistics_filters_by_scope_and_status_without_cross_class_data():
    first = _scope("2026", name="1")
    second = _scope("2026", name="2")
    _submission(first, "approved", ["一班学生"])
    _submission(second, "pending", ["二班学生"])

    context = build_admin_statistics_context({"school_year": "2026", "class_name": "1", "status": "approved"})
    assert [row["class_name"] for row in context["rows"]] == ["1"]
    assert context["rows"][0]["student_names"] == ["一班学生"]

    pending = build_admin_statistics_context({"school_year": "2026", "status": "pending"})
    assert [row["class_name"] for row in pending["rows"]] == ["2"]
    assert pending["rows"][0]["student_names"] == []


@pytest.mark.django_db
def test_exception_workbook_is_numeric_deduplicated_and_formula_safe(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    scope = _scope("2026", name="9")
    submission = _submission(scope, "approved", ["=危险公式", "同名", "同名"])
    students = list(submission.students.order_by("id"))
    students[0].match_status = "ambiguous"
    students[0].is_duplicate = True
    students[0].save(update_fields=["match_status", "is_duplicate"])
    students[1].match_status = "missing"
    students[1].save(update_fields=["match_status"])
    students[2].match_status = "missing"
    students[2].save(update_fields=["match_status"])

    raw = build_exception_workbook({"school_year": "2026", "class_name": "9"})
    book = load_workbook(io.BytesIO(raw), data_only=False)
    sheet = book["异常名单"]
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    assert rows
    assert all(row[1] == "2026" for row in rows)
    assert len(rows) == len({tuple(row) for row in rows})
    assert any(row[0] == "同名学生" for row in rows)
    assert any(row[0] == "待人工确认" for row in rows)
    assert any(row[0] == "缺少照片" for row in rows)
    assert any(row[6] == "'=危险公式" for row in rows)

    stats = build_admin_statistics_context({"school_year": "2026", "class_name": "9"})
    summary = stats["totals"]
    assert isinstance(summary["student_count"], int)
    assert isinstance(summary["matched_photo_count"], int)


@pytest.mark.django_db
def test_exception_export_uses_only_effective_approved_students():
    scope = _scope("2025", department="middle", grade="初二", name="3")
    _submission(scope, "rejected", ["历史异常"])
    current = _submission(scope, "approved", ["当前有照", "当前缺照"])
    raw = build_exception_workbook({"school_year": "2025", "department": "middle", "grade": "初二", "class_name": "3"})
    sheet = load_workbook(io.BytesIO(raw), data_only=False)["异常名单"]
    names = [row[6].value for row in sheet.iter_rows(min_row=2)]
    assert "当前缺照" in names
    assert "历史异常" not in names
    assert current.id
