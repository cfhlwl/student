import hashlib
import io
import json
import os
import re
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from openpyxl import load_workbook
from PIL import Image

from collector.forms import ExportFilterForm
from collector.models import NamingRule, Student, StudentPhoto
from collector.services import ValidationError, create_submission, export_students_to_tempfile, grade_for, review_submission_version, template_bytes

pytestmark = pytest.mark.django_db(transaction=True)


def roster_file(rows, name="名单.xlsx"):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    for row in rows:
        sheet.append(row)
    data = io.BytesIO()
    book.save(data)
    return SimpleUploadedFile(name, data.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def photo_file(name, color=(220, 80, 60), size=(96, 96)):
    data = io.BytesIO()
    Image.new("RGB", size, color).save(data, format="JPEG", quality=88)
    return SimpleUploadedFile(name, data.getvalue(), content_type="image/jpeg")


def document_file(name="通知.pdf"):
    return SimpleUploadedFile(name, b"%PDF-R2-DOCUMENT", content_type="application/pdf")


def large_photo_file(name="大照片.jpg"):
    data = io.BytesIO()
    Image.effect_noise((1800, 1800), 100).convert("RGB").save(data, format="JPEG", quality=96)
    payload = data.getvalue()
    if len(payload) <= 1_100_000:
        payload += os.urandom(1_100_001 - len(payload))
    return SimpleUploadedFile(name, payload, content_type="image/jpeg")


def complete_submission(rows, photos, documents=None):
    submission, _ = create_submission(
        roster_upload=roster_file(rows),
        photo_uploads=photos,
        document_uploads=documents or [],
    )
    return submission


def test_frontend_batches_when_total_size_exceeds_limit():
    source = open("static/js/app.js", encoding="utf-8").read()
    assert re.search(r"photoInput\.files\.length\s*>\s*batchSize\s*\|\|", source)
    assert "files.reduce" in source or "file.size" in source and "batchBytes" in source


def test_ten_classes_submit_twenty_photos_each_without_cross_class_data():
    barrier = threading.Barrier(10)

    def submit_class(index):
        rows = [[f"并发R2-{index:02d}-{n:02d}", "男" if n % 2 else "女", "高中部", "2026", str(index), "住宿", ""] for n in range(20)]
        photos = [photo_file(f"并发R2-{index:02d}-{n:02d}.jpg", (index * 11 % 255, n * 7 % 255, 70)) for n in range(20)]
        barrier.wait(timeout=10)
        return complete_submission(rows, photos)

    with ThreadPoolExecutor(max_workers=10) as pool:
        submissions = list(pool.map(submit_class, range(10)))
    assert len(submissions) == 10
    for index, submission in enumerate(submissions):
        students = list(submission.students.order_by("name"))
        assert len(students) == 20
        assert {student.class_name for student in students} == {str(index)}
        assert submission.photos.count() == 20


def test_upload_session_rejects_changed_photo_manifest():
    client = Client()
    photo = photo_file("清单原图.jpg")
    photo_data = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(photo_data), "sha256": hashlib.sha256(photo_data).hexdigest()}]
    response = client.post(
        reverse("upload-start"),
        data={
            "roster": roster_file([["清单学生", "男", "高中部", "2026", "1", "走读", ""]]),
            "expected_photo_count": "1",
            "photo_manifest": json.dumps(manifest, ensure_ascii=False),
        },
    )
    assert response.status_code == 200
    state = response.json()
    changed = photo_file("清单换图.jpg")
    response = client.post(
        reverse("upload-photo-batch", args=[state["upload_id"]]),
        data={"batch_no": "0", "upload_token": state["upload_token"], "photos": [changed]},
    )
    assert response.status_code == 400


def test_upload_session_rejects_changed_roster_fingerprint():
    client = Client()
    response = client.post(
        reverse("upload-start"),
        data={
            "roster": roster_file([["名单变化", "男", "高中部", "2026", "1", "走读", ""]]),
            "expected_photo_count": "1",
            "roster_fingerprint": "old-roster",
        },
    )
    assert response.status_code == 200
    state = response.json()
    response = client.post(
        reverse("upload-photo-batch", args=[state["upload_id"]]),
        data={
            "batch_no": "0",
            "upload_token": state["upload_token"],
            "roster_fingerprint": "new-roster",
            "photos": [photo_file("名单变化.jpg")],
        },
    )
    assert response.status_code == 400


def test_duplicate_complete_returns_same_result_token_and_old_link_stays_valid():
    client = Client()
    response = client.post(
        reverse("upload-start"),
        data={
            "roster": roster_file([["重复完成", "男", "高中部", "2026", "1", "走读", ""]]),
            "expected_photo_count": "1",
        },
    )
    state = response.json()
    batch = client.post(
        reverse("upload-photo-batch", args=[state["upload_id"]]),
        data={"batch_no": "0", "upload_token": state["upload_token"], "photos": [photo_file("重复完成.jpg")]},
    )
    assert batch.status_code == 200
    first = client.post(reverse("upload-complete", args=[state["upload_id"]]), data={"upload_token": state["upload_token"]}).json()
    second = client.post(reverse("upload-complete", args=[state["upload_id"]]), data={"upload_token": state["upload_token"]})
    assert second.status_code == 200
    assert second.json()["token"] == first["token"]
    assert client.get(first["result_url"]).status_code == 200


def test_single_class_missing_photo_roster_and_document_are_in_class_workbook(tmp_path):
    submission = complete_submission(
        [["单班异常", "男", "高中部", "2026", "1", "住宿", ""]],
        [photo_file("单班异常.jpg")],
        [document_file()],
    )
    admin = User.objects.create(username="r2-admin-missing")
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    submission.refresh_from_db()
    submission.roster_file.delete(save=False)
    submission.documents.get().file.delete(save=False)
    submission.students.get().photo.file.delete(save=False)
    temp_path, _, _ = export_students_to_tempfile({"school_year__name": "2026", "class_name": "1"})
    try:
        with zipfile.ZipFile(temp_path) as archive:
            stats_name = next(name for name in archive.namelist() if name.endswith("/住宿/学生统计表.xlsx"))
            book = load_workbook(io.BytesIO(archive.read(stats_name)), data_only=True)
            anomalies = list(book["异常记录"].values)
            details = "\n".join(str(row) for row in anomalies)
            assert "照片" in details
            assert "学生名单" in details
            assert "相关文档" in details
    finally:
        temp_path.unlink(missing_ok=True)


def test_blank_gender_does_not_use_female_code():
    submission = complete_submission(
        [["性别为空", "", "高中部", "2026", "1", "走读", ""]],
        [photo_file("性别为空.jpg")],
    )
    admin = User.objects.create(username="r2-admin-gender")
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    rule = NamingRule.current()
    rule.mode = "gender_no"
    rule.template = "{姓名}{性别编号}"
    rule.male_code = "01"
    rule.female_code = "02"
    rule.save()
    temp_path, _, _ = export_students_to_tempfile({"school_year__name": "2026"})
    try:
        with zipfile.ZipFile(temp_path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".jpg")]
            assert names == ["高中部/高一/1/走读/学生照片/性别为空.jpg"]
    finally:
        temp_path.unlink(missing_ok=True)


def test_current_school_year_semantics_are_explicit_in_template_and_choices():
    assert grade_for("高中部", "2026") == "高一"
    assert grade_for("高中部", "2025") == "高二"
    assert grade_for("高中部", "2024") == "高三"
    assert set(dict(ExportFilterForm.base_fields["study_type"].choices).keys()) == {"", "走读", "半走读", "住宿"}
    book = load_workbook(io.BytesIO(template_bytes()), data_only=True)
    instructions = " ".join(str(book.active.cell(row, 1).value or "") for row in (1, 2))
    assert "当前在读学年" in instructions


def test_study_type_contract_has_no_non_boarding_value():
    assert [value for value, _ in Student.STUDY_TYPES] == ["走读", "半走读", "住宿"]
    with pytest.raises(ValidationError):
        complete_submission([["旧分类", "男", "高中部", "2026", "1", "非走读", ""]], [photo_file("旧分类.jpg")])


def test_storage_quota_rejects_real_large_file_before_persistence(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.MAX_STORAGE_MB = 1
    with pytest.raises(ValidationError, match="配额"):
        complete_submission([["配额临界", "男", "高中部", "2026", "1", "走读", ""]], [large_photo_file()])
    assert not StudentPhoto.objects.exists()


def test_zip_crc_and_photo_bytes_match_database_hashes(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    rows = [["CRC男", "男", "高中部", "2026", "1", "走读", ""], ["CRC女", "女", "高中部", "2026", "1", "住宿", ""]]
    submission = complete_submission(rows, [photo_file("CRC男.jpg", (20, 40, 60)), photo_file("CRC女.jpg", (80, 100, 120))])
    admin = User.objects.create(username="r2-crc")
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    temp_path, _, _ = export_students_to_tempfile({"school_year__name": "2026"}, actor=admin)
    try:
        with zipfile.ZipFile(temp_path) as archive:
            assert archive.testzip() is None
            for photo in StudentPhoto.objects.filter(submission=submission).select_related("student"):
                name = next(item for item in archive.namelist() if item.endswith("/" + photo.student.name + ".jpg"))
                payload = archive.read(name)
                assert hashlib.sha256(payload).hexdigest() == photo.sha256
                assert archive.getinfo(name).CRC == __import__("binascii").crc32(payload) & 0xFFFFFFFF
    finally:
        temp_path.unlink(missing_ok=True)
