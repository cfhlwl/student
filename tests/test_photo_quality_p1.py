import io
import hashlib
from contextlib import contextmanager

import numpy as np
import pytest
from django.contrib.auth.models import User
from django.db import OperationalError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook
from PIL import Image

from collector import photo_quality, services
from collector.models import AppSetting, AuditLog
from collector.photo_quality import PhotoQualityModelUnavailable, PhotoQualityPolicyError, PhotoQualityResult


class FakeCascade:
    def __init__(self, boxes=()):
        self.boxes = np.asarray(boxes, dtype=np.int32)

    def detectMultiScale(self, image, **kwargs):
        return self.boxes.copy()


def image_upload(*, size=(640, 480), brightness=128, exif_orientation=None):
    rng = np.random.default_rng(42)
    pixels = np.clip(rng.normal(brightness, 35, (size[1], size[0], 3)), 0, 255).astype(np.uint8)
    image = Image.fromarray(pixels)
    output = io.BytesIO()
    if exif_orientation:
        exif = image.getexif()
        exif[274] = exif_orientation
        image.save(output, format="JPEG", exif=exif)
    else:
        image.save(output, format="JPEG")
    return SimpleUploadedFile("synthetic.jpg", output.getvalue(), content_type="image/jpeg")


def fake_models(monkeypatch, *, faces=((200, 60, 240, 360),), profiles=(), eyes=((30, 100, 60, 40), (150, 100, 60, 40))):
    import cv2

    monkeypatch.setattr(photo_quality, "_load_classifiers", lambda: (cv2, FakeCascade(faces), FakeCascade(profiles), FakeCascade(eyes)))


def test_quality_categories_are_distinguished_with_synthetic_inputs(monkeypatch):
    fake_models(monkeypatch)
    assert photo_quality.assess_photo_quality(image_upload()).status == "passed"

    fake_models(monkeypatch, faces=(), profiles=((200, 60, 240, 360),))
    assert photo_quality.assess_photo_quality(image_upload()).code == "profile_face"

    fake_models(monkeypatch, eyes=())
    assert photo_quality.assess_photo_quality(image_upload()).code == "occluded_face"

    fake_models(monkeypatch, faces=((100, 50, 200, 300), (350, 50, 200, 300)))
    assert photo_quality.assess_photo_quality(image_upload()).code == "multiple_faces"

    fake_models(monkeypatch, faces=((280, 190, 64, 64),))
    assert photo_quality.assess_photo_quality(image_upload()).code == "face_too_small"

    fake_models(monkeypatch)
    assert photo_quality.assess_photo_quality(image_upload(brightness=20)).code == "underexposed"
    assert photo_quality.assess_photo_quality(image_upload(brightness=250)).code == "overexposed"

    flat = Image.new("RGB", (640, 480), (128, 128, 128))
    output = io.BytesIO()
    flat.save(output, format="JPEG")
    assert photo_quality.assess_photo_quality(SimpleUploadedFile("blur.jpg", output.getvalue())).code == "blurry"


def test_quality_honours_exif_orientation_and_scales_detection(monkeypatch):
    fake_models(monkeypatch)
    result = photo_quality.assess_photo_quality(image_upload(size=(480, 640), exif_orientation=6))
    assert result.width == 640
    assert result.height == 480
    assert result.detection_width == 640
    assert result.detection_height == 480


def test_detection_failure_and_model_unavailable_are_not_quality_failures(monkeypatch):
    class BrokenCascade(FakeCascade):
        def detectMultiScale(self, image, **kwargs):
            raise RuntimeError("synthetic detector failure")

    import cv2
    monkeypatch.setattr(photo_quality, "_load_classifiers", lambda: (cv2, BrokenCascade(), FakeCascade(), FakeCascade()))
    detection = photo_quality.assess_photo_quality(image_upload())
    assert detection.status == "detection_error"
    assert detection.code == "quality_detection_error"

    monkeypatch.setattr(photo_quality, "_load_classifiers", lambda: (_ for _ in ()).throw(PhotoQualityModelUnavailable("model missing")))
    unavailable = photo_quality.assess_photo_quality(image_upload())
    assert unavailable.status == "model_unavailable"
    assert unavailable.code == "opencv_unavailable"


def test_detection_budget_is_checked_after_eye_detection(monkeypatch):
    fake_models(monkeypatch)
    monkeypatch.setattr(photo_quality, "DETECTION_BUDGET_SECONDS", 0.0)
    result = photo_quality.assess_photo_quality(image_upload())
    assert result.status == "detection_error"
    assert result.code == "detection_budget_exceeded"
    assert result.timeout_mode == "best_effort"


@pytest.mark.django_db
def test_policy_defaults_off_and_database_failure_does_not_fail_open(monkeypatch):
    AppSetting.objects.filter(key="photo_face_quality_blocking").delete()
    assert photo_quality.face_quality_blocking_enabled() is False

    manager = AppSetting.objects
    original_filter = manager.filter
    monkeypatch.setattr(manager, "filter", lambda *args, **kwargs: (_ for _ in ()).throw(OperationalError("database unavailable")))
    with pytest.raises(PhotoQualityPolicyError):
        photo_quality.face_quality_blocking_enabled()
    monkeypatch.setattr(manager, "filter", original_filter)
    assert photo_quality.face_quality_blocking_enabled() is False


@pytest.mark.django_db
def test_enabled_model_failure_is_rejected_by_service(monkeypatch):
    AppSetting.set_value("photo_face_quality_blocking", True)
    monkeypatch.setattr(services, "assess_photo_quality", lambda upload: PhotoQualityResult("model_unavailable", "opencv_unavailable", "model missing"))
    with pytest.raises(services.ValidationError, match="人脸质量检查"):
        services._validate_photo_quality([services.PreparedUpload("synthetic.jpg", "synthetic.jpg", "image/jpeg", 1, "sha", source=io.BytesIO(b"x"))])


@pytest.mark.django_db
def test_toggle_audit_and_setting_are_atomic_on_audit_failure(client, monkeypatch):
    admin = User.objects.create_user(username="toggle-failure", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    AppSetting.objects.filter(key="photo_face_quality_blocking").delete()
    original_create = AuditLog.objects.create

    def fail_create(*args, **kwargs):
        raise RuntimeError("audit database failure")

    monkeypatch.setattr(AuditLog.objects, "create", fail_create)
    response = client.post(reverse("toggle-photo-quality"))
    assert response.status_code == 302
    assert AppSetting.objects.filter(key="photo_face_quality_blocking").exists() is False
    monkeypatch.setattr(AuditLog.objects, "create", original_create)


@pytest.mark.django_db
def test_quality_detection_runs_before_collection_write_gate(monkeypatch):
    AppSetting.set_value("photo_face_quality_blocking", True)
    state = {"in_gate": False, "checked_outside": False}

    @contextmanager
    def fake_gate():
        state["in_gate"] = True
        try:
            yield
        finally:
            state["in_gate"] = False

    monkeypatch.setattr(services, "collection_write_gate", fake_gate)

    def pass_quality(upload):
        assert state["in_gate"] is False
        state["checked_outside"] = True
        return PhotoQualityResult("passed", "", "合格")

    monkeypatch.setattr(services, "assess_photo_quality", pass_quality)
    prepared = services.PreparedUpload("synthetic.jpg", "synthetic.jpg", "image/jpeg", 1, "sha", source=io.BytesIO(b"x"))
    assert services._validate_photo_quality([prepared])
    assert state["checked_outside"] is True


@pytest.mark.django_db
def test_finalize_hashes_each_staged_photo_once(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    AppSetting.set_value("photo_face_quality_blocking", True)

    roster_book = Workbook()
    roster_sheet = roster_book.active
    roster_sheet.append(["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型"])
    roster_sheet.append(["锁内校验", "男", "高中部", "2026", "高一", "P1", "走读"])
    roster_bytes = io.BytesIO()
    roster_book.save(roster_bytes)
    roster = SimpleUploadedFile("名单.xlsx", roster_bytes.getvalue())
    photo = image_upload()
    photo_bytes = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(photo_bytes), "sha256": hashlib.sha256(photo_bytes).hexdigest()}]
    session, token, _ = services.start_upload_session(
        roster_upload=roster,
        document_uploads=[],
        expected_photo_count=1,
        photo_manifest=manifest,
        roster_fingerprint="synthetic-roster",
    )
    services.stage_upload_batch(
        session.id, "photo", 0, [photo], token=token,
        manifest_digest=session.photo_manifest_digest,
        roster_fingerprint="synthetic-roster",
    )
    monkeypatch.setattr(services, "assess_photo_quality", lambda upload: PhotoQualityResult("passed", "", "合格"))
    calls = {"count": 0}
    original_assert = services._assert_quality_evidence

    def counted_assert(prepared, evidence):
        calls["count"] += 1
        return original_assert(prepared, evidence)

    monkeypatch.setattr(services, "_assert_quality_evidence", counted_assert)
    submission, _ = services.finalize_upload_session(
        session_id=session.id, token=token,
        manifest_digest=session.photo_manifest_digest,
        roster_fingerprint="synthetic-roster",
    )
    assert submission.photos.count() == 1
    assert calls["count"] == 1
