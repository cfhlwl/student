import hashlib
import io
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from openpyxl import Workbook

from collector.models import PhotoSupplement, StudentPhoto, SupplementPhoto
from collector.services import ValidationError, create_photo_supplement, create_submission, finalize_upload_session, match_supplement_photo, review_photo_supplement, review_submission_version, stage_upload_batch, start_upload_session, upload_session_status


def roster_upload(name="名单.xlsx", student_name="R3同学"):
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    sheet.append([student_name, "男", "高中部", "2026", "1", "走读", ""])
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
    admin = User.objects.create_user(username="r3-admin", password="strong-r3-password", is_staff=True)
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    supplement, matched, ambiguous = create_photo_supplement(submission_id=submission.id, token=token, photo_uploads=[photo_upload()])
    assert supplement.status == "pending"
    assert (matched, ambiguous) == (1, 0)
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
def test_r3_same_files_get_distinct_random_upload_sessions(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    first, first_token, _ = start_upload_session(roster_upload=roster_upload(), document_uploads=[], idempotency_key="1" * 64)
    finalized, _ = finalize_upload_session(session_id=first.id, token=first_token, manifest_digest=first.photo_manifest_digest, roster_fingerprint=first.roster_fingerprint)
    assert finalized is not None
    second, second_token, _ = start_upload_session(roster_upload=roster_upload(), document_uploads=[], idempotency_key="2" * 64)
    assert second.id != first.id
    assert second_token != first_token


@pytest.mark.django_db
def test_r3_same_idempotency_key_rejects_changed_roster_metadata(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    start_upload_session(roster_upload=roster_upload(student_name="原名单"), document_uploads=[], idempotency_key="3" * 64, roster_fingerprint="first")
    with pytest.raises(ValidationError, match="清单不一致"):
        start_upload_session(roster_upload=roster_upload(student_name="改名单"), document_uploads=[], idempotency_key="3" * 64, roster_fingerprint="second")


@pytest.mark.django_db
def test_r3_status_endpoint_uses_post_body_and_no_store(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client = Client()
    response = client.post(reverse("upload-start"), data={"roster": roster_upload(), "expected_photo_count": "0"})
    state = response.json()
    status = client.post(reverse("upload-status", args=[state["upload_id"]]), data={"upload_token": state["upload_token"]})
    assert status.status_code == 200
    assert status["Cache-Control"].startswith("no-store")
    assert status["Pragma"] == "no-cache"
    assert client.get(reverse("upload-status", args=[state["upload_id"]]) + "?upload_token=" + state["upload_token"]).status_code == 405


def test_r3_f5_nonce_requires_crypto_random_values():
    source = (Path(__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "getRandomValues" in source
    assert "Math.random" not in source
    assert "不支持安全随机数" in source
    assert "idempotencyKey: randomNonce()" in source


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


@pytest.mark.django_db
def test_r3_manual_supplement_binding_rejects_duplicate_and_supports_unbind(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission, token = create_submission(roster_upload=roster_upload(), photo_uploads=[], document_uploads=[])
    supplement, _, _ = create_photo_supplement(submission_id=submission.id, token=token, photo_uploads=[photo_upload("未知甲.jpg"), photo_upload("未知乙.jpg")])
    student = submission.students.get()
    first, second = list(SupplementPhoto.objects.filter(supplement=supplement).order_by("id"))
    match_supplement_photo(photo_id=first.id, student_id=student.id)
    with pytest.raises(ValidationError, match="同一补传"):
        match_supplement_photo(photo_id=second.id, student_id=student.id)
    cleared = match_supplement_photo(photo_id=first.id, student_id=None)
    assert cleared.student_id is None
    assert cleared.match_status == "ambiguous"


@pytest.mark.django_db
def test_r3_stale_supplement_is_expired_after_new_class_version(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    admin = User.objects.create_user(username="r3-version-admin", password="strong-r3-password", is_staff=True)
    old_submission, old_token = create_submission(roster_upload=roster_upload(), photo_uploads=[], document_uploads=[])
    review_submission_version(submission_id=old_submission.id, action="approved", actor=admin)
    supplement, _, _ = create_photo_supplement(submission_id=old_submission.id, token=old_token, photo_uploads=[photo_upload()])
    new_submission, _ = create_submission(roster_upload=roster_upload(), photo_uploads=[], document_uploads=[])
    review_submission_version(submission_id=new_submission.id, action="approved", actor=admin)
    result = review_photo_supplement(supplement_id=supplement.id, action="approved", actor=admin)
    assert result.status == "expired"
    assert StudentPhoto.objects.filter(submission=old_submission).count() == 0
