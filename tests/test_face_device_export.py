import hashlib
import io
import zipfile

import cv2
import numpy as np
import pytest
from django.core.files.base import ContentFile
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from openpyxl import load_workbook
from PIL import Image

from collector import face_export
from collector.export_jobs import build_export_fingerprint
from collector.face_export import DevicePhotoError, prepare_device_photo
from collector.models import ClassGroup, NamingRule, SchoolYear, Student, StudentPhoto, Submission
from collector.services import export_students_to_tempfile


class DynamicCascade:
    def __init__(self, result):
        self.result = result

    def detectMultiScale(self, image, **kwargs):
        if callable(self.result):
            result = self.result(image)
        else:
            result = self.result
        return np.asarray(result, dtype=np.int32)


def dynamic_face(image):
    height, width = image.shape[:2]
    return [(width // 3, height // 5, width // 3, height // 2)]


def fake_models(monkeypatch, *, faces=dynamic_face, profiles=(), eyes=lambda image: [(2, 2, 20, 20), (40, 2, 20, 20)]):
    monkeypatch.setattr(
        face_export,
        "_load_classifiers",
        lambda: (cv2, DynamicCascade(faces), DynamicCascade(profiles), DynamicCascade(eyes)),
    )


def noisy_jpeg(size=(1600, 2200), *, orientation=None, quality=96):
    rng = np.random.default_rng(7)
    pixels = rng.integers(40, 216, (size[1], size[0], 3), dtype=np.uint8)
    image = Image.fromarray(pixels, "RGB")
    out = io.BytesIO()
    kwargs = {"format": "JPEG", "quality": quality, "subsampling": 0}
    if orientation is not None:
        exif = image.getexif()
        exif[274] = orientation
        kwargs["exif"] = exif.tobytes()
    image.save(out, **kwargs)
    return out.getvalue()


def approved_student(name, *, class_name="1"):
    year, _ = SchoolYear.objects.get_or_create(name="2026")
    group, _ = ClassGroup.objects.get_or_create(department="high", school_year=year, grade="高一", name=class_name)
    submission = Submission.objects.create(
        access_token_hash=hashlib.sha256(f"{name}-{Student.objects.count()}".encode("utf-8")).hexdigest(),
        department="high", school_year=year, grade="高一", class_group=group,
        status="approved", source_filename="名单.xlsx",
    )
    return Student.objects.create(
        submission=submission, name=name, gender="男", department="high",
        school_year=year, grade="高一", class_name=class_name, study_type="走读", match_status="matched",
    )


def add_photo(student, data, original_name):
    StudentPhoto.objects.create(
        student=student, submission=student.submission, original_name=original_name,
        file=ContentFile(data, name=original_name), content_type="image/jpeg",
        size=len(data), sha256=hashlib.sha256(data).hexdigest(),
    )


@pytest.mark.django_db
def test_device_photo_exif_crop_and_compression(monkeypatch, tmp_path):
    fake_models(monkeypatch)
    source_bytes = noisy_jpeg(orientation=6)
    source = tmp_path / "large-source.jpg"
    source.write_bytes(source_bytes)
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    output, metrics = prepare_device_photo(source, tmp_path / "out")

    assert output.suffix == ".jpg"
    assert output.stat().st_size <= 500 * 1024
    assert metrics.face_width >= 128
    assert metrics.face_height >= 128
    assert metrics.face_height / metrics.output_height >= 1 / 3
    with Image.open(output) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.getexif().get(274) in (None, 1)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("size", "quality"),
    [((320, 240), 50), ((400, 300), 60)],
)
def test_qualified_small_jpegs_are_copied_without_reencoding(monkeypatch, tmp_path, size, quality):
    fake_models(monkeypatch, faces=lambda image: [(50, 10, 150, 150)])
    source = tmp_path / f"small-{quality}.jpg"
    source_bytes = noisy_jpeg(size=size, quality=quality)
    assert len(source_bytes) <= (50 if quality == 50 else 200) * 1024
    source.write_bytes(source_bytes)

    output, metrics = prepare_device_photo(source, tmp_path / "out")

    assert output.read_bytes() == source_bytes
    assert metrics.output_size == len(source_bytes)
    assert metrics.jpeg_quality == 0


@pytest.mark.django_db
def test_qualified_large_jpeg_is_scaled_without_cropping(monkeypatch, tmp_path):
    fake_models(monkeypatch, faces=lambda image: [(500, 10, 500, 900)])
    source = tmp_path / "large-full-frame.jpg"
    source.write_bytes(noisy_jpeg(size=(1250, 1850)))
    assert source.stat().st_size > 5 * 1024 * 1024

    output, _ = prepare_device_photo(source, tmp_path / "out")

    with Image.open(source) as original, Image.open(output) as converted:
        assert output.stat().st_size <= 500 * 1024
        assert converted.size[0] / converted.size[1] == pytest.approx(original.size[0] / original.size[1], rel=0.02)


@pytest.mark.django_db
def test_large_upper_body_photo_is_cropped_only_when_face_ratio_is_small(monkeypatch, tmp_path):
    fake_models(monkeypatch, faces=lambda image: [(550, 500, 400, 400)])
    source = tmp_path / "upper-body.jpg"
    source.write_bytes(noisy_jpeg(size=(1600, 2200)))
    assert source.stat().st_size > 8_000_000

    output, _ = prepare_device_photo(source, tmp_path / "out")

    with Image.open(source) as original, Image.open(output) as converted:
        assert output.stat().st_size <= 500 * 1024
        assert converted.size[0] / converted.size[1] != pytest.approx(original.size[0] / original.size[1], rel=0.02)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("faces", "profiles", "code"),
    [([], [], "no_face"), ([(1, 1, 100, 100), (200, 1, 100, 100)], [], "multiple_faces"), ([], [(1, 1, 100, 100)], "profile_face"), ([(1, 1, 20, 20)], [], "face_too_small")],
)
def test_device_photo_rejects_unsafe_face_cases(monkeypatch, tmp_path, faces, profiles, code):
    fake_models(monkeypatch, faces=faces, profiles=profiles)
    source = tmp_path / "source.jpg"
    source.write_bytes(noisy_jpeg())
    with pytest.raises(DevicePhotoError) as exc_info:
        prepare_device_photo(source, tmp_path / "out")
    assert exc_info.value.code == code


@pytest.mark.django_db
def test_mixed_frontal_and_distinct_profile_faces_are_rejected(monkeypatch, tmp_path):
    fake_models(
        monkeypatch,
        faces=[(200, 200, 400, 500)],
        profiles=[(1100, 250, 300, 420)],
    )
    source = tmp_path / "mixed-faces.jpg"
    source.write_bytes(noisy_jpeg())

    with pytest.raises(DevicePhotoError) as exc_info:
        prepare_device_photo(source, tmp_path / "out")
    assert exc_info.value.code == "multiple_faces"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "faces",
    [
        [(100, 100, 200, 200), (200, 100, 200, 200)],
        [(200, 100, 200, 200), (100, 100, 200, 200)],
    ],
)
def test_partially_overlapping_frontal_faces_are_rejected_in_any_order(monkeypatch, tmp_path, faces):
    fake_models(monkeypatch, faces=faces)
    source = tmp_path / "partial-overlap.jpg"
    source.write_bytes(noisy_jpeg())

    with pytest.raises(DevicePhotoError) as exc_info:
        prepare_device_photo(source, tmp_path / "out")
    assert exc_info.value.code == "multiple_faces"


@pytest.mark.django_db
def test_adjacent_partially_overlapping_profile_face_is_rejected(monkeypatch, tmp_path):
    fake_models(
        monkeypatch,
        faces=[(200, 200, 400, 500)],
        profiles=[(500, 250, 300, 420)],
    )
    source = tmp_path / "adjacent-profile.jpg"
    source.write_bytes(noisy_jpeg())

    with pytest.raises(DevicePhotoError) as exc_info:
        prepare_device_photo(source, tmp_path / "out")
    assert exc_info.value.code == "multiple_faces"


@pytest.mark.django_db
def test_overlapping_profile_box_is_deduplicated(monkeypatch, tmp_path):
    fake_models(
        monkeypatch,
        faces=[(200, 200, 400, 500)],
        profiles=[(220, 210, 380, 480)],
    )
    source = tmp_path / "same-face-detectors.jpg"
    source.write_bytes(noisy_jpeg())

    output, _ = prepare_device_photo(source, tmp_path / "out")
    assert output.is_file()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "faces",
    [
        [(200, 200, 400, 500), (220, 210, 380, 480)],
        [(220, 210, 380, 480), (200, 200, 400, 500)],
    ],
)
def test_same_face_high_overlap_deduplication_is_order_independent(monkeypatch, tmp_path, faces):
    fake_models(monkeypatch, faces=faces)
    source = tmp_path / "same-face-order.jpg"
    source.write_bytes(noisy_jpeg())

    output, _ = prepare_device_photo(source, tmp_path / "out")
    assert output.is_file()


@pytest.mark.django_db
def test_device_export_isolated_fingerprint_and_jpg_collision_protection(monkeypatch, settings, tmp_path):
    fake_models(monkeypatch)
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    data = noisy_jpeg()
    first = approved_student("同名学生")
    second = Student.objects.create(
        submission=first.submission, name="同名学生", gender="男", department="high",
        school_year=first.school_year, grade="高一", class_name="1", study_type="走读", match_status="matched",
    )
    add_photo(first, data, "同名学生.png")
    add_photo(second, data, "同名学生.jpg")
    NamingRule.objects.create(mode="name", template="{姓名}")

    original_fp, _ = build_export_fingerprint({})
    device_fp, _ = build_export_fingerprint({"export_mode": "face_device"})
    assert original_fp != device_fp

    path, count, _ = export_students_to_tempfile({"export_mode": "face_device"})
    try:
        assert count == 2
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            photos = sorted(name for name in archive.namelist() if "/学生照片/" in name)
            assert [name.rsplit("/", 1)[-1] for name in photos] == ["同名学生.jpg", "同名学生_2.jpg"]
            assert all(name.endswith(".jpg") for name in photos)
            statistics = [name for name in archive.namelist() if name.endswith("学生统计表.xlsx")][0]
            workbook = load_workbook(io.BytesIO(archive.read(statistics)), read_only=True)
            rows = list(workbook["学生明细"].values)
            exported_names = {row[6] for row in rows[1:]}
            assert exported_names == {"同名学生.jpg", "同名学生_2.jpg"}
    finally:
        path.unlink(missing_ok=True)
    assert hashlib.sha256(first.photo.file.read()).hexdigest() == first.photo.sha256


@pytest.mark.django_db
def test_admin_exposes_both_export_modes_and_preserves_filters(client):
    admin = User.objects.create_user(username="face-export-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    page = client.get(reverse("admin-students"))
    content = page.content.decode()
    assert reverse("export-zip") in content
    assert reverse("export-face-zip") in content

    response = client.get(reverse("export-face-zip") + "?grade=%E9%AB%98%E4%B8%80", HTTP_ACCEPT="application/json")
    assert response.status_code == 202
    assert response.json()["job_id"]
