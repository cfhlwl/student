import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.contrib.auth.models import User
from django.urls import reverse
from openpyxl import load_workbook
from PIL import Image

from collector import services
from collector.export_jobs import claim_next_export_job, enqueue_export_job, process_export_job
from collector.face_export import DevicePhotoError
from collector.maintenance import write_state
from collector.models import ClassGroup, ExportJob, SchoolYear, Student, StudentPhoto, Submission
from collector.services import export_students_to_tempfile


def _image_bytes(format_name="JPEG"):
    image = Image.new("RGB", (640, 480), (96, 128, 160))
    out = io.BytesIO()
    if format_name == "MPO":
        second = Image.new("RGB", image.size, (160, 128, 96))
        image.save(out, format="MPO", save_all=True, append_images=[second])
    else:
        image.save(out, format=format_name)
    return out.getvalue()


def _large_image_bytes(format_name):
    image = Image.new("RGB", (6001, 4000), (96, 128, 160))
    out = io.BytesIO()
    image.save(out, format=format_name)
    return out.getvalue()


def _approved_group():
    year = SchoolYear.objects.create(name="2097")
    group = ClassGroup.objects.create(department="high", school_year=year, grade="高一", name="回退")
    submission = Submission.objects.create(
        access_token_hash="f" * 64,
        department="high",
        school_year=year,
        grade="高一",
        class_group=group,
        status="approved",
        source_filename="合成名单.xlsx",
    )
    return year, submission


def _student(submission, year, name, number):
    return Student.objects.create(
        submission=submission,
        student_no=str(number),
        name=name,
        gender="男",
        department="high",
        school_year=year,
        grade="高一",
        class_name="回退",
        study_type="走读",
        match_status="matched",
    )


def _photo(student, data, name):
    return StudentPhoto.objects.create(
        student=student,
        submission=student.submission,
        original_name=name,
        file=ContentFile(data, name=name),
        content_type="image/jpeg",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


@pytest.mark.django_db
def test_face_device_failed_preprocess_keeps_verified_original_and_accounts_outputs(monkeypatch, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    year, submission = _approved_group()
    fallback_bytes = _image_bytes("PNG")
    ready_bytes = _image_bytes("JPEG")
    fallback_student = _student(submission, year, "回退照片", 1)
    ready_student = _student(submission, year, "设备照片", 2)
    _photo(fallback_student, fallback_bytes, "回退照片.png")
    _photo(ready_student, ready_bytes, "设备照片.jpg")
    fallback_digest = hashlib.sha256(fallback_bytes).hexdigest()

    def fake_prepare(source_path, output_dir):
        if hashlib.sha256(Path(source_path).read_bytes()).hexdigest() == fallback_digest:
            raise DevicePhotoError("multiple_faces", "检测到多张人脸，不能自动选择其中一张。")
        output = Path(output_dir) / "synthetic-device.jpg"
        output.write_bytes(b"device-ready-jpg")
        return output, None

    monkeypatch.setattr(services, "prepare_device_photo", fake_prepare)
    metrics = {}
    path, count, _ = export_students_to_tempfile({"export_mode": "face_device"}, export_metrics=metrics)
    try:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            photos = [name for name in archive.namelist() if "/学生照片/" in name or "/待处理原图/" in name]
            assert len(photos) == count == 2
            assert sum("/学生照片/" in name for name in photos) == 1
            assert sum("/待处理原图/" in name for name in photos) == 1
            fallback_member = next(name for name in photos if "/待处理原图/" in name)
            assert fallback_member.endswith(".png")
            assert archive.read(fallback_member) == fallback_bytes
            statistics = next(name for name in archive.namelist() if name.endswith("学生统计表.xlsx"))
            workbook = load_workbook(io.BytesIO(archive.read(statistics)), read_only=True, data_only=True)
            summary = {row[0]: row[1] for row in workbook["统计汇总"].iter_rows(values_only=True) if row[0]}
            assert summary["设备合格 JPG 数"] == 1
            assert summary["待处理原图数"] == 1
            assert summary["源照片不可用数"] == 0
    finally:
        path.unlink(missing_ok=True)
    assert metrics == {
        "processed_students": 2,
        "exported_photo_count": 1,
        "fallback_original_count": 1,
        "source_unavailable_count": 0,
        "rejection_reasons": {"multiple_faces": 1},
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("format_name", "extension", "error_code"),
    [("JPEG", ".jpg", "jpeg_validation"), ("PNG", ".png", "unsupported_image"), ("MPO", ".mpo", "device_photo_error")],
)
def test_readable_source_falls_back_for_device_output_failures(monkeypatch, settings, tmp_path, format_name, extension, error_code):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    year, submission = _approved_group()
    source_bytes = _image_bytes(format_name)
    student = _student(submission, year, f"兜底{format_name}", 1)
    _photo(student, source_bytes, f"兜底{format_name}{extension}")

    def fail_device_conversion(source_path, output_dir):
        raise DevicePhotoError(error_code, "合成设备处理失败")

    monkeypatch.setattr(services, "prepare_device_photo", fail_device_conversion)
    metrics = {}
    path, _, _ = export_students_to_tempfile({"export_mode": "face_device"}, export_metrics=metrics)
    try:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            members = [name for name in archive.namelist() if "/待处理原图/" in name]
            assert len(members) == 1
            assert members[0].endswith(extension)
            assert archive.read(members[0]) == source_bytes
    finally:
        path.unlink(missing_ok=True)
    assert metrics["fallback_original_count"] == 1
    assert metrics["source_unavailable_count"] == 0
    assert metrics["rejection_reasons"] == {error_code: 1}


@pytest.mark.django_db
@pytest.mark.parametrize("format_name,extension", [("JPEG", ".jpg"), ("PNG", ".png")])
def test_over_budget_source_fallback_uses_metadata_without_full_decode(monkeypatch, settings, tmp_path, format_name, extension):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    year, submission = _approved_group()
    source_bytes = _large_image_bytes(format_name)
    student = _student(submission, year, f"超限{format_name}", 1)
    _photo(student, source_bytes, f"超限{format_name}{extension}")

    def fail_device_budget(source_path, output_dir):
        raise DevicePhotoError("image_pixel_budget", "图片像素超过设备检测预算")

    load_calls = []
    original_load = Image.Image.load

    def record_load(image, *args, **kwargs):
        load_calls.append(image.size)
        return original_load(image, *args, **kwargs)

    monkeypatch.setattr(services, "prepare_device_photo", fail_device_budget)
    monkeypatch.setattr(Image.Image, "load", record_load)
    metrics = {}
    path, _, _ = export_students_to_tempfile({"export_mode": "face_device"}, export_metrics=metrics)
    try:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            member = next(name for name in archive.namelist() if "/待处理原图/" in name)
            assert member.endswith(extension)
            retained = archive.read(member)
            assert hashlib.sha256(retained).hexdigest() == hashlib.sha256(source_bytes).hexdigest()
            assert retained == source_bytes
    finally:
        path.unlink(missing_ok=True)
    assert load_calls == []
    assert metrics["fallback_original_count"] == 1
    assert metrics["source_unavailable_count"] == 0


@pytest.mark.django_db
def test_face_device_missing_source_is_not_faked_as_fallback(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    year, submission = _approved_group()
    student = _student(submission, year, "缺失照片", 1)
    photo = _photo(student, b"source-that-will-be-removed", "缺失照片.jpg")
    Path(photo.file.path).unlink()
    metrics = {}
    path, _, _ = export_students_to_tempfile({"export_mode": "face_device"}, export_metrics=metrics)
    try:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            assert not any("学生照片/" in name or "待处理原图/" in name for name in archive.namelist())
    finally:
        path.unlink(missing_ok=True)
    assert metrics["fallback_original_count"] == 0
    assert metrics["source_unavailable_count"] == 1
    assert metrics["rejection_reasons"] == {"source_unavailable": 1}


@pytest.mark.django_db
def test_face_device_corrupt_or_hash_mismatch_is_not_faked_as_fallback(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    year, submission = _approved_group()
    corrupt_student = _student(submission, year, "损坏照片", 1)
    mismatch_student = _student(submission, year, "摘要不符", 2)
    _photo(corrupt_student, b"not-an-image", "损坏照片.jpg")
    mismatch = _photo(mismatch_student, b"valid-bytes-for-hash-test", "摘要不符.jpg")
    mismatch.sha256 = "0" * 64
    mismatch.save(update_fields=["sha256"])
    metrics = {}
    path, _, _ = export_students_to_tempfile({"export_mode": "face_device"}, export_metrics=metrics)
    try:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            assert not any("学生照片/" in name or "待处理原图/" in name for name in archive.namelist())
    finally:
        path.unlink(missing_ok=True)
    assert metrics["fallback_original_count"] == 0
    assert metrics["source_unavailable_count"] == 2
    assert metrics["rejection_reasons"] == {"source_unavailable": 2}


@pytest.mark.django_db
def test_export_job_persists_photo_accounting_and_status_api(monkeypatch, client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    year, submission = _approved_group()
    student = _student(submission, year, "任务统计", 1)
    _photo(student, _image_bytes("JPEG"), "任务统计.jpg")

    def reject(source_path, output_dir):
        raise DevicePhotoError("face_too_small", "人脸区域过小。")

    monkeypatch.setattr(services, "prepare_device_photo", reject)
    job = enqueue_export_job({"export_mode": "face_device"})
    process_export_job(claim_next_export_job())
    job.refresh_from_db()
    assert job.status == "completed"
    assert job.exported_photo_count == 0
    assert job.fallback_original_count == 1
    assert job.source_unavailable_count == 0
    assert job.rejection_reasons == {"face_too_small": 1}

    admin = User.objects.create_user(username="accounting-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    response = client.get(reverse("admin-export-job-status", args=[job.pk]))
    assert response.status_code == 200
    assert response.json()["photo_accounting_known"] is True
    assert response.json()["fallback_original_count"] == 1


@pytest.mark.django_db
def test_export_download_list_reports_photo_accounting(client):
    job = ExportJob.objects.create(
        fingerprint="l" * 64,
        data_snapshot="s" * 64,
        status="completed",
        student_count=4,
        exported_photo_count=2,
        fallback_original_count=1,
        source_unavailable_count=1,
        filters={"export_mode": "face_device"},
    )
    admin = User.objects.create_user(username="export-list-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    response = client.get(reverse("admin-exports"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "已处理 4 人" in body
    assert "实际输出照片 3 张" in body
    assert "设备合格 JPG 2 张" in body
    assert "待处理原图 1 张" in body
    assert "源文件不可用 1 张" in body


@pytest.mark.django_db
def test_historical_export_without_accounting_is_explicitly_unknown(client):
    job = ExportJob.objects.create(
        fingerprint="h" * 64,
        data_snapshot="s" * 64,
        status="completed",
        student_count=1075,
    )
    admin = User.objects.create_user(username="historical-accounting-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    response = client.get(reverse("admin-export-job-status", args=[job.pk]))
    assert response.status_code == 200
    assert response.json()["photo_accounting_known"] is False
    assert response.json()["exported_photo_count"] is None
