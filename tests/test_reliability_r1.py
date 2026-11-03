import hashlib
import io
import threading
import time
import tracemalloc
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from openpyxl import Workbook, load_workbook
from PIL import Image

from collector.models import NamingRule, Student, StudentPhoto, Submission
from collector.services import create_submission, export_students_to_tempfile, review_submission_version


def roster_file(rows, name="名单.xlsx"):
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    for row in rows:
        sheet.append(row)
    data = io.BytesIO()
    book.save(data)
    return SimpleUploadedFile(name, data.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def photo_file(name, color=(220, 80, 60)):
    data = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(data, format="JPEG")
    return SimpleUploadedFile(name, data.getvalue(), content_type="image/jpeg")


def document_file(name="通知.pdf", content=b"%PDF-R1-DOCUMENT"):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def batch_submit(client, rows, photos):
    response = client.post(reverse("upload-start"), data={"roster": roster_file(rows), "expected_photo_count": str(len(photos)), "note": "R1虚构测试"})
    assert response.status_code == 200, response.content
    state = response.json()
    session_id = state["upload_id"]
    token = state["upload_token"]
    for batch_no in range(0, len(photos), 20):
        batch = photos[batch_no:batch_no + 20]
        response = client.post(
            reverse("upload-photo-batch", args=[session_id]),
            data={"batch_no": str(batch_no // 20), "upload_token": token, "photos": batch},
        )
        assert response.status_code == 200, response.content
    response = client.post(reverse("upload-complete", args=[session_id]), data={"upload_token": token})
    assert response.status_code == 200, response.content
    return Submission.objects.get(pk=response.json()["submission_id"])


@pytest.mark.django_db
@pytest.mark.parametrize("photo_count", [50, 100, 300])
def test_batch_upload_counts_disk_files_and_sha256(settings, client, tmp_path, photo_count):
    settings.MEDIA_ROOT = tmp_path
    rows = [[f"批量学生{i:03d}", "男" if i % 2 else "女", "高中部", "2026", "1", "走读", ""] for i in range(photo_count)]
    photos = [photo_file(f"批量学生{i:03d}.jpg", (i % 255, 80, 60)) for i in range(photo_count)]
    submission = batch_submit(client, rows, photos)
    assert submission.students.count() == photo_count
    assert submission.photos.count() == photo_count
    for student in submission.students.select_related("photo"):
        with student.photo.file.open("rb") as source:
            assert hashlib.sha256(source.read()).hexdigest() == student.photo.sha256


@pytest.mark.django_db
def test_batch_retry_is_idempotent_and_can_complete(settings, client, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    rows = [["可重试", "男", "高中部", "2026", "1", "走读", ""]]
    response = client.post(reverse("upload-start"), data={"roster": roster_file(rows), "expected_photo_count": "1"})
    state = response.json()
    batch_data = {"batch_no": "0", "upload_token": state["upload_token"], "photos": [photo_file("可重试.jpg")]}
    first = client.post(reverse("upload-photo-batch", args=[state["upload_id"]]), data=batch_data)
    retry = client.post(reverse("upload-photo-batch", args=[state["upload_id"]]), data={"batch_no": "0", "upload_token": state["upload_token"], "photos": [photo_file("可重试.jpg")]})
    assert first.status_code == retry.status_code == 200
    submission = client.post(reverse("upload-complete", args=[state["upload_id"]]), data={"upload_token": state["upload_token"]}).json()
    assert Student.objects.filter(submission_id=submission["submission_id"]).count() == 1
    assert StudentPhoto.objects.filter(submission_id=submission["submission_id"]).count() == 1


@pytest.mark.django_db
def test_staged_upload_survives_new_client_connection(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    first_client = Client()
    response = first_client.post(reverse("upload-start"), data={"roster": roster_file([["重启后完成", "男", "高中部", "2026", "1", "走读", ""]]), "expected_photo_count": "1"})
    state = response.json()
    assert first_client.post(reverse("upload-photo-batch", args=[state["upload_id"]]), data={"batch_no": "0", "upload_token": state["upload_token"], "photos": [photo_file("重启后完成.jpg")] }).status_code == 200
    second_client = Client()
    completed = second_client.post(reverse("upload-complete", args=[state["upload_id"]]), data={"upload_token": state["upload_token"]})
    assert completed.status_code == 200
    assert Student.objects.filter(submission_id=completed.json()["submission_id"]).count() == 1


@pytest.mark.django_db(transaction=True)
def test_ten_class_submissions_concurrent_do_not_drop_or_cross_wire(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    barrier = threading.Barrier(10)

    def submit(index):
        from django.db import close_old_connections
        from collector.services import create_submission
        close_old_connections()
        barrier.wait()
        submission, _ = create_submission(
            roster_upload=roster_file([[f"并发学生{index}", "男", "高中部", "2026", str(index), "住宿", ""]]),
            photo_uploads=[photo_file(f"并发学生{index}.jpg", (index, 90, 60))],
            document_uploads=[],
        )
        close_old_connections()
        return submission.id

    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(submit, range(10)))
    assert len(set(ids)) == 10
    assert Submission.objects.filter(id__in=ids).count() == 10
    assert StudentPhoto.objects.filter(submission_id__in=ids).count() == 10


@pytest.mark.django_db(transaction=True)
def test_same_class_approval_competition_leaves_one_adopted_version(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    first = batch_submit(Client(), [["版本一", "男", "高中部", "2026", "1", "走读", ""]], [photo_file("版本一.jpg")])
    second = batch_submit(Client(), [["版本二", "女", "高中部", "2026", "1", "走读", ""]], [photo_file("版本二.jpg")])
    admin = User.objects.create_user(username="r1-review", password="strong-password-123", is_staff=True)

    def approve(submission_id):
        return review_submission_version(submission_id=submission_id, action="approved", actor=admin)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(approve, [first.id, second.id]))
    assert Submission.objects.filter(class_group=first.class_group, status="approved").count() == 1


@pytest.mark.django_db
def test_missing_photo_is_exported_as_anomaly_and_zip_stream_is_cleaned(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    submission = batch_submit(Client(), [["文件缺失学生", "男", "高中部", "2026", "1", "走读", ""]], [photo_file("文件缺失学生.jpg")])
    admin = User.objects.create_user(username="r1-export", password="strong-password-123", is_staff=True)
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    photo = submission.students.get().photo
    photo.file.delete(save=False)
    temp_path, count, _ = export_students_to_tempfile({"school_year__name": "2026"}, actor=admin)
    with zipfile.ZipFile(temp_path) as archive:
        stats_name = next(name for name in archive.namelist() if name.endswith("学生统计表.xlsx"))
        book = load_workbook(io.BytesIO(archive.read(stats_name)), data_only=True)
        anomaly_values = list(book["异常记录"].values)
        assert any("文件缺失" in str(cell) for row in anomaly_values for cell in row if cell is not None)
        assert not any(name.endswith("文件缺失学生.jpg") for name in archive.namelist())
    temp_path.unlink(missing_ok=True)
    assert not temp_path.exists()


@pytest.mark.django_db
def test_export_summary_counts_are_numeric(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    submission = batch_submit(Client(), [["数值学生", "男", "高中部", "2026", "1", "住宿", ""]], [photo_file("数值学生.jpg")])
    admin = User.objects.create_user(username="r1-numeric", password="strong-password-123", is_staff=True)
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    temp_path, _, _ = export_students_to_tempfile({"school_year__name": "2026"}, actor=admin)
    with zipfile.ZipFile(temp_path) as archive:
        stats_name = next(name for name in archive.namelist() if name.endswith("学生统计表.xlsx"))
        book = load_workbook(io.BytesIO(archive.read(stats_name)), data_only=True)
        assert isinstance(book["统计汇总"]["B2"].value, int)
        assert isinstance(book["统计汇总"]["B8"].value, int)
    temp_path.unlink(missing_ok=True)


@pytest.mark.django_db
def test_export_keeps_roster_and_document_bytes_and_gender_naming(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    roster = [["男生", "男", "高中部", "2026", "1", "走读", ""], ["女生", "女", "高中部", "2026", "1", "走读", ""]]
    submission = create_submission(
        roster_upload=roster_file(roster),
        photo_uploads=[photo_file("男生.jpg"), photo_file("女生.jpg")],
        document_uploads=[document_file()],
    )[0]
    admin = User.objects.create_user(username="r1-bytes", password="strong-password-123", is_staff=True)
    rule = NamingRule.current()
    rule.mode = "gender_no"
    rule.template = "{姓名}{性别编号}"
    rule.male_code = "01"
    rule.female_code = "02"
    rule.save()
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    temp_path, _, _ = export_students_to_tempfile({"school_year__name": "2026"}, actor=admin)
    with zipfile.ZipFile(temp_path) as archive:
        names = archive.namelist()
        assert any(name.endswith("男生01.jpg") for name in names)
        assert any(name.endswith("女生02.jpg") for name in names)
        document_name = next(name for name in names if name.endswith("通知.pdf"))
        assert archive.read(document_name) == b"%PDF-R1-DOCUMENT"
        roster_name = next(name for name in names if name.endswith("学生名单/名单.xlsx"))
        assert archive.read(roster_name)[:2] == b"PK"
    temp_path.unlink(missing_ok=True)


@pytest.mark.django_db
def test_large_export_reports_time_and_peak_memory(settings, client, tmp_path, capsys):
    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    rows = [[f"导出学生{i:03d}", "男", "高中部", "2026", "1", "走读", ""] for i in range(300)]
    photos = [photo_file(f"导出学生{i:03d}.jpg", (i % 255, 100, 70)) for i in range(300)]
    submission = batch_submit(client, rows, photos)
    admin = User.objects.create_user(username="r1-metrics", password="strong-password-123", is_staff=True)
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    tracemalloc.start()
    started = time.perf_counter()
    temp_path, count, _ = export_students_to_tempfile({"school_year__name": "2026"}, actor=admin)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"EXPORT_METRICS count={count} seconds={elapsed:.3f} peak_bytes={peak}")
    assert count == 300
    assert peak < 64 * 1024 * 1024
    temp_path.unlink(missing_ok=True)
