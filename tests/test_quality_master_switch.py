import io
import time
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook
from PIL import Image

from collector import preflight, services
from collector.models import AppSetting, AuditLog
from collector.photo_quality import PhotoQualityPolicy, PhotoQualityResult, read_face_quality_policy


def roster_upload():
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型"])
    sheet.append(["测试学生", "男", "高中部", "2026", "高一", "1", "走读"])
    data = io.BytesIO()
    book.save(data)
    return SimpleUploadedFile("名单.xlsx", data.getvalue())


def photo_upload():
    data = io.BytesIO()
    Image.new("RGB", (180, 180), (120, 120, 120)).save(data, format="JPEG")
    return SimpleUploadedFile("测试学生.jpg", data.getvalue(), content_type="image/jpeg")


def synthetic_photos(count):
    photos = []
    for index in range(count):
        data = io.BytesIO()
        Image.new("RGB", (180, 180), (80 + index % 40, 120, 150)).save(data, format="JPEG", quality=85)
        photos.append(SimpleUploadedFile(f"学生{index:03d}.jpg", data.getvalue(), content_type="image/jpeg"))
    return photos


@pytest.mark.django_db
def test_master_switch_defaults_off_and_preserves_legacy_blocking():
    AppSetting.objects.all().delete()
    policy = read_face_quality_policy()
    assert (policy.detection_enabled, policy.blocking_enabled) == (False, False)

    AppSetting.set_value("photo_face_quality_blocking", True)
    policy = read_face_quality_policy()
    assert (policy.detection_enabled, policy.blocking_enabled) == (True, True)

    AppSetting.set_value("photo_face_quality_detection_enabled", False)
    policy = read_face_quality_policy()
    assert (policy.detection_enabled, policy.blocking_enabled) == (False, False)


@pytest.mark.django_db
def test_detection_master_off_skips_opencv_and_quality_warnings(monkeypatch):
    AppSetting.objects.all().delete()
    calls = {"count": 0}

    def fail_if_called(upload):
        calls["count"] += 1
        raise AssertionError("OpenCV must not run while detection is disabled")

    monkeypatch.setattr(services, "assess_photo_quality", fail_if_called)
    assert services._validate_photo_quality([
        services.PreparedUpload("测试学生.jpg", "测试学生.jpg", "image/jpeg", 1, "sha", source=io.BytesIO(b"x"))
    ]) == []

    monkeypatch.setattr(preflight, "assess_photo_quality", fail_if_called)
    result = preflight.run_preflight(roster_upload(), [photo_upload()], quality_policy=read_face_quality_policy())
    assert calls["count"] == 0
    assert not result.warnings
    assert result.photos[0].quality_status == "not_checked"


@pytest.mark.django_db
def test_detection_on_nonblocking_reports_warning_and_allows_service(monkeypatch):
    AppSetting.set_value("photo_face_quality_detection_enabled", True)
    AppSetting.set_value("photo_face_quality_blocking", False)
    failed = PhotoQualityResult("failed", "face_too_small", "人脸区域过小")
    monkeypatch.setattr(preflight, "assess_photo_quality", lambda upload: failed)
    result = preflight.run_preflight(roster_upload(), [photo_upload()], quality_policy=read_face_quality_policy())
    assert result.warnings[0].level == "warning"
    assert result.warnings[0].filename == "测试学生.jpg"

    monkeypatch.setattr(services, "assess_photo_quality", lambda upload: failed)
    evidence = services._validate_photo_quality([
        services.PreparedUpload("测试学生.jpg", "测试学生.jpg", "image/jpeg", 1, "sha", source=io.BytesIO(b"x"))
    ])
    assert evidence[0]["quality"]["status"] == "failed"
    assert evidence[0]["blocking_enabled"] is False


@pytest.mark.django_db
def test_detection_and_blocking_on_rejects_direct_service_submission(monkeypatch):
    AppSetting.set_value("photo_face_quality_detection_enabled", True)
    AppSetting.set_value("photo_face_quality_blocking", True)
    failed = PhotoQualityResult("failed", "face_too_small", "人脸区域过小")
    monkeypatch.setattr(services, "assess_photo_quality", lambda upload: failed)
    with pytest.raises(services.ValidationError, match="人脸质量检查"):
        services._validate_photo_quality([
            services.PreparedUpload("测试学生.jpg", "测试学生.jpg", "image/jpeg", 1, "sha", source=io.BytesIO(b"x"))
        ])


@pytest.mark.django_db
def test_admin_detection_toggle_is_atomic_and_disables_blocking(client):
    admin = User.objects.create_user(username="master-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    AppSetting.set_value("photo_face_quality_detection_enabled", True)
    AppSetting.set_value("photo_face_quality_blocking", True)

    response = client.post(reverse("toggle-photo-quality-detection"))
    assert response.status_code == 302
    assert read_face_quality_policy().detection_enabled is False
    assert read_face_quality_policy().blocking_enabled is False
    assert AuditLog.objects.filter(action="toggle_photo_face_quality_detection").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("count", [100, 300])
def test_detection_off_zero_opencv_calls_for_batch_sizes(monkeypatch, count):
    calls = {"count": 0}

    def fail_if_called(upload):
        calls["count"] += 1
        raise AssertionError("OpenCV must not run while detection is disabled")

    monkeypatch.setattr(preflight, "assess_photo_quality", fail_if_called)
    started = time.perf_counter()
    result = preflight.run_preflight(
        roster_upload(), synthetic_photos(count),
        quality_policy=PhotoQualityPolicy(detection_enabled=False),
    )
    elapsed = time.perf_counter() - started
    print(f"MASTER_OFF count={count} opencv_calls={calls['count']} elapsed_ms={elapsed * 1000:.1f}")
    assert calls["count"] == 0
    assert not [issue for issue in result.warnings if issue.code.startswith(("face_", "no_face", "quality", "blurry", "underexposed", "overexposed"))]
    assert all(photo.quality_status == "not_checked" for photo in result.photos)
    monkeypatch.setattr(services, "assess_photo_quality", fail_if_called)
    prepared = [
        services.PreparedUpload(f"学生{index:03d}.jpg", f"学生{index:03d}.jpg", "image/jpeg", 1, f"sha-{index}", source=io.BytesIO(b"x"))
        for index in range(count)
    ]
    assert services._validate_photo_quality(prepared) == []
    assert calls["count"] == 0


def test_detection_on_reports_separate_detection_time(monkeypatch):
    original = preflight.assess_photo_quality
    metrics = {"count": 0, "seconds": 0.0}

    def measured(upload):
        started = time.perf_counter()
        try:
            return original(upload)
        finally:
            metrics["count"] += 1
            metrics["seconds"] += time.perf_counter() - started

    photos = synthetic_photos(100)
    started = time.perf_counter()
    monkeypatch.setattr(preflight, "assess_photo_quality", measured)
    result = preflight.run_preflight(
        roster_upload(), photos,
        quality_policy=PhotoQualityPolicy(detection_enabled=True),
    )
    elapsed = time.perf_counter() - started
    print(f"MASTER_ON count=100 opencv_calls={metrics['count']} detection_ms={metrics['seconds'] * 1000:.1f} total_ms={elapsed * 1000:.1f}")
    assert metrics["count"] == len(result.photos) == 100


def test_upload_ui_uses_inline_results_and_keeps_admin_clear_confirmation():
    app = Path("static/js/app.js").read_text(encoding="utf-8")
    submit = Path("templates/collector/submit.html").read_text(encoding="utf-8")
    dashboard = Path("templates/collector/admin_dashboard.html").read_text(encoding="utf-8")
    assert "window.confirm" not in app
    assert "window.alert" not in app
    assert "upload-preflight-result" in submit
    assert "photo_quality_detection" in dashboard
    assert "confirm('此操作将先备份" in dashboard
