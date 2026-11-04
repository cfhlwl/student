import io
import zipfile

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import load_workbook

from collector.services import create_submission, export_students
from collector.models import StagedUpload, Submission, UploadBatch, UploadSession
from collector.backup import clear_collection_safely
from tests.test_core import excel_upload, image_upload


@pytest.mark.django_db
def test_public_preflight_route_returns_validation_summary(client):
    response = client.post(
        reverse("submit-preflight"),
        {
            "roster": excel_upload([["预检查同学", "男", "高中部", "2026", "1", "走读", ""]]),
            "photos": [image_upload("预检查同学.jpg")],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["student_count"] == 1
    assert payload["summary"]["matched_count"] == 1
    assert payload["blocking_errors"] == []


@pytest.mark.django_db
def test_admin_statistics_and_exception_export_are_protected(client):
    admin = User.objects.create_user(username="r3-admin", password="strong-password-123", is_staff=True)
    assert client.get(reverse("admin-statistics")).status_code == 302
    assert client.login(username="r3-admin", password="strong-password-123")
    page = client.get(reverse("admin-statistics"))
    assert page.status_code == 200
    export = client.get(reverse("admin-statistics-exceptions"))
    assert export.status_code == 200
    book = load_workbook(io.BytesIO(export.content), read_only=True)
    assert book.sheetnames == ["异常名单"]


@pytest.mark.django_db
def test_same_class_name_in_two_school_years_has_separate_export_directories(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    first, _ = create_submission(
        roster_upload=excel_upload([["旧学年同学", "男", "高中部", "2025", "1", "走读", ""]]),
        photo_uploads=[image_upload("旧学年同学.jpg")], document_uploads=[],
    )
    second, _ = create_submission(
        roster_upload=excel_upload([["新学年同学", "女", "高中部", "2026", "1", "住宿", ""]]),
        photo_uploads=[image_upload("新学年同学.jpg")], document_uploads=[],
    )
    first.status = second.status = "approved"
    first.save(update_fields=["status"])
    second.save(update_fields=["status"])
    payload, count, _ = export_students({})
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
    assert count == 2
    assert any(name.startswith("2025/高中部/") for name in names)
    assert any(name.startswith("2026/高中部/") for name in names)


@pytest.mark.django_db
def test_admin_clear_button_requires_confirmation_and_preserves_admin(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.COLLECTION_BACKUP_DIR = tmp_path / "backups"
    admin = User.objects.create_user(username="clear-admin", password="strong-password-123", is_staff=True)
    create_submission(
        roster_upload=excel_upload([["待清除", "男", "高中部", "2026", "1", "走读", ""]]),
        photo_uploads=[image_upload("待清除.jpg")], document_uploads=[],
    )
    assert client.login(username="clear-admin", password="strong-password-123")
    denied = client.post(reverse("clear-test-data"))
    assert denied.status_code == 302
    assert Submission.objects.exists()
    cleared = client.post(reverse("clear-test-data"), {"confirm_clear": "YES"})
    assert cleared.status_code == 302
    assert not Submission.objects.exists()
    assert User.objects.filter(pk=admin.pk).exists()
    assert list((settings.COLLECTION_BACKUP_DIR).glob("before-clear-*.zip"))


@pytest.mark.django_db
def test_safe_clear_removes_upload_sessions_and_staged_files(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    session = UploadSession.objects.create(
        access_token_hash="b" * 64,
        start_idempotency_key="clear-session-1",
        roster_file=SimpleUploadedFile("临时名单.xlsx", b"roster"),
        roster_original_name="临时名单.xlsx",
    )
    batch = UploadBatch.objects.create(session=session, kind="photo", sequence=0, digest="c" * 64, count=1)
    StagedUpload.objects.create(
        batch=batch,
        ordinal=0,
        original_name="临时.jpg",
        file=SimpleUploadedFile("临时.jpg", b"photo"),
        content_type="image/jpeg",
        size=5,
        sha256="d" * 64,
    )
    clear_collection_safely(tmp_path / "backup.zip", media_root=settings.MEDIA_ROOT)
    assert UploadSession.objects.count() == 0
    assert UploadBatch.objects.count() == 0
    assert StagedUpload.objects.count() == 0
