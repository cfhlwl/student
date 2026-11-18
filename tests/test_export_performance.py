import hashlib
import zipfile
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from collector.export_jobs import _ThrottledExportProgress
from collector.models import ClassGroup, SchoolYear, Student, StudentPhoto, Submission
from collector.services import export_students_to_tempfile


def _synthetic_export_fixture(tmp_path, count=12):
    media = tmp_path / "media"
    export_dir = tmp_path / "exports"
    media.mkdir()
    export_dir.mkdir()
    content = b"synthetic-photo-content" * 200
    digest = hashlib.sha256(content).hexdigest()
    year = SchoolYear.objects.create(name="2098")
    class_group = ClassGroup.objects.create(department="high", school_year=year, grade="高一", name="性能")
    submission = Submission.objects.create(
        access_token_hash="a" * 64,
        department="high",
        school_year=year,
        grade="高一",
        class_group=class_group,
        status="approved",
        source_filename="synthetic.xlsx",
    )
    students = [
        Student(
            submission=submission,
            student_no=f"{index:04d}",
            name=f"合成学生{index:04d}",
            gender="男" if index % 2 else "女",
            department="high",
            school_year=year,
            grade="高一",
            class_name="性能",
            study_type="走读",
            match_status="matched",
        )
        for index in range(count)
    ]
    Student.objects.bulk_create(students)
    photos = []
    for index, student in enumerate(Student.objects.filter(submission=submission).order_by("id")):
        relative = Path("submissions", str(submission.id), "photos", f"{index:04d}.jpg")
        target = media / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        photos.append(
            StudentPhoto(
                student=student,
                submission=submission,
                original_name=relative.name,
                file=relative.as_posix(),
                content_type="image/jpeg",
                size=len(content),
                sha256=digest,
            )
        )
    StudentPhoto.objects.bulk_create(photos)
    return media, export_dir


@pytest.mark.django_db(transaction=True)
def test_original_export_uses_one_photo_query_and_preserves_zip_content(settings, tmp_path):
    media, export_dir = _synthetic_export_fixture(tmp_path)
    settings.EXPORT_PHOTO_WORKERS = 2
    settings.EXPORT_PHOTO_MAX_INFLIGHT = 4
    with override_settings(MEDIA_ROOT=media, EXPORT_TEMP_DIR=export_dir):
        with CaptureQueriesContext(connection) as queries:
            path, count, _ = export_students_to_tempfile({"export_mode": "original"})
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            photo_members = [name for name in archive.namelist() if name.endswith(".jpg")]
            assert len(photo_members) == count == 12
        path.unlink()
    photo_queries = [query for query in queries if "studentphoto" in query["sql"].lower()]
    assert len(photo_queries) == 1


@pytest.mark.django_db
def test_export_progress_writes_are_throttled_but_finish_exactly(monkeypatch, settings):
    calls = []
    monkeypatch.setattr("collector.export_jobs._update_export_progress", lambda *args: calls.append(args[2:]))
    settings.EXPORT_PROGRESS_UPDATE_MIN_STEP = 10
    settings.EXPORT_PROGRESS_UPDATE_MIN_INTERVAL_SECONDS = 999
    progress = _ThrottledExportProgress(object(), "token")
    for processed in range(101):
        progress(processed, 100)
    assert [call[0] for call in calls] == list(range(0, 101, 10))
    assert calls[-1] == (100, 100)
