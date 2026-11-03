import hashlib
import io

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from openpyxl import Workbook

from collector.models import PhotoSupplement, StudentPhoto
from collector.services import ValidationError, create_photo_supplement, create_submission, review_photo_supplement, stage_upload_batch, start_upload_session, upload_session_status


def roster_upload(name="名单.xlsx"):
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    sheet.append(["R3同学", "男", "高中部", "2026", "1", "走读", ""])
    data = io.BytesIO()
    book.save(data)
    return SimpleUploadedFile(name, data.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def photo_upload(name="R3同学.jpg"):
    data = io.BytesIO()
    Image.new("RGB", (48, 48), (20, 90, 140)).save(data, format="JPEG")
    return SimpleUploadedFile(name, data.getvalue(), content_type="image/jpeg")


def document_upload(name="通知.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.7\nR3 test document\n", content_type="application/pdf")


@pytest.mark.django_db
def test_r3_status_requires_token_and_returns_confirmed_hashes(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    photo = photo_upload()
    raw = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]
    session, token, _ = start_upload_session(roster_upload=roster_upload(), document_uploads=[], expected_photo_count=1, photo_manifest=manifest, roster_fingerprint="roster-hash")
    with pytest.raises(ValidationError, match="上传凭证"):
        upload_session_status(session_id=session.id, token="wrong", manifest_digest=session.photo_manifest_digest, roster_fingerprint="roster-hash")
    stage_upload_batch(session.id, "photo", 0, [photo], token=token, manifest_digest=session.photo_manifest_digest, roster_fingerprint="roster-hash")
    status = upload_session_status(session_id=session.id, token=token, manifest_digest=session.photo_manifest_digest, roster_fingerprint="roster-hash")
    assert status["confirmed_photo_count"] == 1
    assert status["batches"][0]["uploads"][0]["sha256"] == manifest[0]["sha256"]


@pytest.mark.django_db
def test_r3_supplement_is_pending_until_admin_approval(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission, token = create_submission(roster_upload=roster_upload(), photo_uploads=[], document_uploads=[])
    supplement, matched, ambiguous = create_photo_supplement(submission_id=submission.id, token=token, photo_uploads=[photo_upload()])
    assert supplement.status == "pending"
    assert (matched, ambiguous) == (1, 0)
    admin = User.objects.create_user(username="r3-admin", password="strong-r3-password", is_staff=True)
    review_photo_supplement(supplement_id=supplement.id, action="approved", actor=admin)
    assert PhotoSupplement.objects.get(pk=supplement.id).status == "approved"
    assert StudentPhoto.objects.filter(submission=submission).count() == 1


@pytest.mark.django_db
def test_r3_supplement_token_is_submission_scoped(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission, token = create_submission(roster_upload=roster_upload(), photo_uploads=[], document_uploads=[])
    with pytest.raises(ValidationError, match="访问凭证"):
        create_photo_supplement(submission_id=submission.id, token="not-the-token", photo_uploads=[photo_upload()])


@pytest.mark.django_db
def test_r3_start_retry_is_idempotent(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    first, first_token, first_count = start_upload_session(roster_upload=roster_upload(), document_uploads=[], idempotency_key="a" * 64)
    second, second_token, second_count = start_upload_session(roster_upload=roster_upload(), document_uploads=[], idempotency_key="a" * 64)
    assert second.id == first.id
    assert second_token == first_token
    assert (second_count, first_count) == (1, 1)


@pytest.mark.django_db
def test_r3_documents_can_be_staged_after_start_without_mixing_into_start_request(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    document = document_upload()
    raw = document.read()
    document.seek(0)
    manifest = [{"name": document.name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]
    session, token, _ = start_upload_session(
        roster_upload=roster_upload(), document_uploads=[], expected_document_count=1,
        document_manifest=manifest, roster_fingerprint="r3-roster",
    )
    stage_upload_batch(session.id, "document", 0, [document], token=token, manifest_digest="", roster_fingerprint="r3-roster")
    status = upload_session_status(session_id=session.id, token=token, roster_fingerprint="r3-roster")
    assert status["confirmed_document_count"] == 1
    assert status["document_manifest"] == manifest
    assert status["batches"][0]["uploads"][0]["sha256"] == manifest[0]["sha256"]
