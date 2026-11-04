import hashlib
import io
import zipfile

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import load_workbook

from collector.models import ClassGroup, SchoolYear, Student, StudentPhoto, Submission
from collector.services import export_students


def _approved_students(count=125):
    year = SchoolYear.objects.create(name="2098")
    group = ClassGroup.objects.create(department="high", school_year=year, grade="高一", name="分页测试班")
    submission = Submission.objects.create(
        access_token_hash=hashlib.sha256(b"pagination-submission").hexdigest(),
        department="high",
        school_year=year,
        grade="高一",
        class_group=group,
        source_filename="虚构名单.xlsx",
        status="approved",
    )
    matched_payloads = {}
    for index in range(count):
        if index % 3 == 0:
            status = "matched"
        elif index % 3 == 1:
            status = "missing"
        else:
            status = "ambiguous"
        student = Student.objects.create(
            submission=submission,
            name=f"分页学生{index:03d}",
            gender="男" if index % 2 == 0 else "女",
            department="high",
            school_year=year,
            grade="高一",
            class_name="分页测试班",
            study_type=("走读", "半走读", "住宿")[index % 3],
            match_status=status,
        )
        if status == "matched":
            payload = f"fictional-photo-{index}".encode()
            matched_payloads[student.name] = payload
            StudentPhoto.objects.create(
                student=student,
                submission=submission,
                original_name=f"{student.name}.jpg",
                file=SimpleUploadedFile(f"{student.name}.jpg", payload, content_type="image/jpeg"),
                content_type="image/jpeg",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
    return year, submission, matched_payloads


@pytest.mark.django_db
def test_admin_students_pagination_sizes_filters_and_photo_query_budget(settings, tmp_path, client):
    settings.MEDIA_ROOT = tmp_path / "pagination-media"
    _approved_students()
    admin = User.objects.create_user(username="pagination-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)

    page_lengths = []
    seen = []
    for page in (1, 2, 3):
        with CaptureQueriesContext(connection) as captured:
            response = client.get(reverse("admin-students"), {"page": page, "page_size": 50, "department": "high"})
        assert response.status_code == 200
        assert response.context["student_page"].number == page
        rows = list(response.context["student_page"].object_list)
        page_lengths.append(len(rows))
        seen.extend(student.pk for student in rows)
        photo_lookup_queries = [
            query["sql"].lower()
            for query in captured.captured_queries
            if "collector_studentphoto" in query["sql"].lower() and 'student_id" =' in query["sql"].lower()
        ]
        assert photo_lookup_queries == []
    assert page_lengths == [50, 50, 25]
    assert len(seen) == len(set(seen)) == 125

    for page_size, expected_pages in ((100, 2), (200, 1)):
        response = client.get(reverse("admin-students"), {"page_size": page_size})
        assert response.context["student_page"].paginator.num_pages == expected_pages
    invalid_page = client.get(reverse("admin-students"), {"page": "not-a-number", "page_size": 50})
    assert invalid_page.context["student_page"].number == 1
    out_of_range = client.get(reverse("admin-students"), {"page": 999, "page_size": 50})
    assert out_of_range.context["student_page"].number == 3
    assert "department=high" in client.get(
        reverse("admin-students"), {"page": 2, "page_size": 50, "department": "high"}
    ).context["pagination_query"]
    filtered = client.get(reverse("admin-students"), {"page": 2, "page_size": 50, "department": "high"})
    assert "page=" not in filtered.context["export_query"]
    assert "page_size=" not in filtered.context["export_query"]


@pytest.mark.django_db
def test_export_all_paged_dataset_keeps_all_rows_photos_and_numeric_summary(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    _, _, expected_payloads = _approved_students()
    payload, count, _ = export_students({})

    assert count == 125
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.testzip() is None
        photo_members = [name for name in archive.namelist() if "/学生照片/" in name]
        stats_members = [name for name in archive.namelist() if name.endswith("/学生统计表.xlsx")]
        assert len(photo_members) == len(expected_payloads)
        assert len(stats_members) == 3
        for member in photo_members:
            filename = member.rsplit("/", 1)[-1]
            name = filename.rsplit(".", 1)[0]
            assert archive.read(member) == expected_payloads[name]

        detail_rows = 0
        summary_photo_count = 0
        for member in stats_members:
            book = load_workbook(io.BytesIO(archive.read(member)), read_only=True, data_only=True)
            summary = book["统计汇总"]
            detail = book["学生明细"]
            detail_rows += detail.max_row - 1
            summary_photo_count += summary["B8"].value
            assert all(isinstance(summary[f"B{row}"].value, (int, float)) for row in range(2, 10))
        assert detail_rows == 125
        assert summary_photo_count == len(expected_payloads)
