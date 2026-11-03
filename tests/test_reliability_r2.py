import hashlib
import io
import json
import os
import re
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from PIL import Image

from collector.forms import ExportFilterForm
from collector.models import AppSetting, ClassGroup, NamingRule, SchoolYear, StagedUpload, Student, StudentPhoto, Submission, UploadBatch, UploadSession
from collector import services
from collector.services import ValidationError, create_submission, export_students_to_tempfile, grade_for, review_submission_version, template_bytes

pytestmark = pytest.mark.django_db(transaction=True)


def roster_file(rows, name="名单.xlsx"):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "备注"] if rows and len(rows[0]) == 8 else ["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    for row in rows:
        sheet.append(row)
    data = io.BytesIO()
    book.save(data)
    return SimpleUploadedFile(name, data.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def explicit_roster_file(row, name="明确年级名单.xlsx"):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "备注"])
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
    assert "photoInput.files.length > batchSize" in source
    assert "totalPhotoBytes > batchBytes" in source
    assert "totalPhotoBytes + totalDocumentBytes + rosterBytes > batchBytes" in source
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


def test_photo_batch_requires_token_and_has_no_side_effect(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client = Client()
    photo = photo_file("令牌照片.jpg")
    photo_data = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(photo_data), "sha256": hashlib.sha256(photo_data).hexdigest()}]
    response = client.post(
        reverse("upload-start"),
        data={"roster": roster_file([["令牌学生", "男", "高中部", "2026", "高一", "1", "走读", ""]]), "expected_photo_count": "1", "photo_manifest": json.dumps(manifest, ensure_ascii=False)},
    )
    state = response.json()
    session_root = tmp_path / "upload_sessions" / state["upload_id"]
    before = sorted(str(path.relative_to(session_root)) for path in session_root.rglob("*") if path.is_file())
    for token in (None, "not-the-token"):
        data = {"batch_no": "0", "photos": [photo_file("令牌照片.jpg")], "manifest_digest": state["photo_manifest_digest"]}
        if token is not None:
            data["upload_token"] = token
        failed = client.post(reverse("upload-photo-batch", args=[state["upload_id"]]), data=data)
        assert failed.status_code == 400
    after = sorted(str(path.relative_to(session_root)) for path in session_root.rglob("*") if path.is_file())
    assert before == after
    assert not UploadBatch.objects.exists()
    assert not StagedUpload.objects.exists()


def test_photo_manifest_is_required_for_nonempty_session(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    response = Client().post(
        reverse("upload-start"),
        data={"roster": roster_file([["清单必填", "男", "高中部", "2026", "高一", "1", "走读", ""]]), "expected_photo_count": "1"},
    )
    assert response.status_code == 400
    assert not UploadSession.objects.exists()


def test_upload_session_rejects_changed_roster_fingerprint():
    client = Client()
    photo = photo_file("名单变化.jpg")
    photo_data = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(photo_data), "sha256": hashlib.sha256(photo_data).hexdigest()}]
    response = client.post(
        reverse("upload-start"),
        data={
            "roster": roster_file([["名单变化", "男", "高中部", "2026", "1", "走读", ""]]),
            "expected_photo_count": "1",
            "photo_manifest": json.dumps(manifest, ensure_ascii=False),
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
            "manifest_digest": state["photo_manifest_digest"],
            "roster_fingerprint": "new-roster",
            "photos": [photo_file("名单变化.jpg")],
        },
    )
    assert response.status_code == 400


def test_duplicate_complete_returns_same_result_token_and_old_link_stays_valid():
    client = Client()
    photo = photo_file("重复完成.jpg")
    photo_data = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(photo_data), "sha256": hashlib.sha256(photo_data).hexdigest()}]
    response = client.post(
        reverse("upload-start"),
        data={
            "roster": roster_file([["重复完成", "男", "高中部", "2026", "1", "走读", ""]]),
            "expected_photo_count": "1",
            "photo_manifest": json.dumps(manifest, ensure_ascii=False),
        },
    )
    state = response.json()
    batch = client.post(
        reverse("upload-photo-batch", args=[state["upload_id"]]),
        data={"batch_no": "0", "upload_token": state["upload_token"], "manifest_digest": state["photo_manifest_digest"], "photos": [photo_file("重复完成.jpg")]},
    )
    assert batch.status_code == 200
    first = client.post(reverse("upload-complete", args=[state["upload_id"]]), data={"upload_token": state["upload_token"], "manifest_digest": state["photo_manifest_digest"]}).json()
    second = client.post(reverse("upload-complete", args=[state["upload_id"]]), data={"upload_token": state["upload_token"], "manifest_digest": state["photo_manifest_digest"]})
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


def test_same_current_school_year_keeps_all_six_grades_and_export_paths(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    admin = User.objects.create(username="r2-six-grades")
    grade_rows = [
        ("初一", "初中部"), ("初二", "初中部"), ("初三", "初中部"),
        ("高一", "高中部"), ("高二", "高中部"), ("高三", "高中部"),
    ]
    submissions = []
    for index, (grade, department) in enumerate(grade_rows):
        name = f"同学年{grade}"
        class_name = f"F1-{index + 1}"
        submission, _ = create_submission(
            roster_upload=explicit_roster_file([name, "男", department, "2026", grade, class_name, "走读", ""]),
            photo_uploads=[photo_file(name + ".jpg")],
            document_uploads=[],
        )
        review_submission_version(submission_id=submission.id, action="approved", actor=admin)
        submissions.append(submission)
    temp_path, count, _ = export_students_to_tempfile({"school_year__name": "2026"}, actor=admin)
    try:
        with zipfile.ZipFile(temp_path) as archive:
            names = set(archive.namelist())
            assert count == 6
            for grade, department in grade_rows:
                assert any(name.startswith(f"{department}/{grade}/") and name.endswith(".jpg") for name in names)
    finally:
        temp_path.unlink(missing_ok=True)


def test_configured_scope_uses_explicit_grade_for_same_school_year():
    AppSetting.set_value("collection_scope", "configured")
    year = SchoolYear.objects.create(name="2026")
    ClassGroup.objects.create(department="high", school_year=year, grade="高二", name="配置高二", is_active=True)
    try:
        submission = complete_submission(
            [["配置高二学生", "男", "高中部", "2026", "高二", "配置高二", "走读", ""]],
            [photo_file("配置高二学生.jpg")],
        )
        assert submission.grade == "高二"
        assert submission.class_group.grade == "高二"
    finally:
        AppSetting.set_value("collection_scope", "all")


def test_finalize_recovers_after_interruption_after_submission_create(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    client = Client()
    photo = photo_file("恢复提交.jpg")
    photo_data = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(photo_data), "sha256": hashlib.sha256(photo_data).hexdigest()}]
    started = client.post(
        reverse("upload-start"),
        data={"roster": roster_file([["恢复提交", "男", "高中部", "2026", "高一", "1", "走读", ""]]), "expected_photo_count": "1", "photo_manifest": json.dumps(manifest, ensure_ascii=False)},
    ).json()
    assert client.post(reverse("upload-photo-batch", args=[started["upload_id"]]), data={"batch_no": "0", "upload_token": started["upload_token"], "manifest_digest": started["photo_manifest_digest"], "photos": [photo_file("恢复提交.jpg")] }).status_code == 200
    original = services._mark_upload_session_completed
    calls = {"count": 0}

    def fail_once(session_id, submission):
        if calls["count"] == 0:
            calls["count"] += 1
            raise RuntimeError("模拟进程中断")
        return original(session_id, submission)

    monkeypatch.setattr(services, "_mark_upload_session_completed", fail_once)
    with pytest.raises(RuntimeError, match="模拟进程中断"):
        services.finalize_upload_session(session_id=started["upload_id"], token=started["upload_token"], manifest_digest=started["photo_manifest_digest"])
    session = UploadSession.objects.get(pk=started["upload_id"])
    assert session.status == "pending"
    assert Submission.objects.count() == 1
    recovered, token = services.finalize_upload_session(session_id=started["upload_id"], token=started["upload_token"], manifest_digest=started["photo_manifest_digest"])
    assert recovered.id == Submission.objects.get().id
    assert token == services._session_result_token(started["upload_id"])
    assert UploadSession.objects.get(pk=started["upload_id"]).status == "completed"
    assert Submission.objects.count() == 1


def test_finalize_removes_partial_submission_and_orphan_tree_before_retry(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client = Client()
    photo = photo_file("半成品恢复.jpg")
    photo_data = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(photo_data), "sha256": hashlib.sha256(photo_data).hexdigest()}]
    started = client.post(
        reverse("upload-start"),
        data={"roster": roster_file([["半成品恢复", "男", "高中部", "2026", "高一", "1", "走读", ""]]), "expected_photo_count": "1", "photo_manifest": json.dumps(manifest, ensure_ascii=False)},
    ).json()
    assert client.post(reverse("upload-photo-batch", args=[started["upload_id"]]), data={"batch_no": "0", "upload_token": started["upload_token"], "manifest_digest": started["photo_manifest_digest"], "photos": [photo_file("半成品恢复.jpg")] }).status_code == 200
    session = UploadSession.objects.get(pk=started["upload_id"])
    with session.roster_file.open("rb") as source:
        records = services.parse_roster(SimpleUploadedFile("名单.xlsx", source.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    school_year, class_group = services._class_from_record(records[0])
    stable_token = services._session_result_token(session.id)
    partial = Submission.objects.create(
        access_token_hash=hashlib.sha256(stable_token.encode()).hexdigest(),
        department=class_group.department, school_year=school_year, grade=class_group.grade,
        class_group=class_group, source_filename="半成品.xlsx",
    )
    orphan = tmp_path / "submissions" / str(partial.id) / "photos" / "孤儿.jpg"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    recovered, token = services.finalize_upload_session(session_id=session.id, token=started["upload_token"], manifest_digest=started["photo_manifest_digest"])
    assert recovered.id != partial.id
    assert token == stable_token
    assert not Submission.objects.filter(pk=partial.id).exists()
    assert not orphan.exists()
    assert UploadSession.objects.get(pk=session.id).status == "completed"


def _finalizing_partial_session(client, name="finalizing半成品"):
    photo = photo_file(name + ".jpg")
    photo_data = photo.read()
    photo.seek(0)
    manifest = [{"name": photo.name, "size": len(photo_data), "sha256": hashlib.sha256(photo_data).hexdigest()}]
    response = client.post(
        reverse("upload-start"),
        data={"roster": roster_file([[name, "男", "高中部", "2026", "高一", "F6", "走读", ""]]), "expected_photo_count": "1", "photo_manifest": json.dumps(manifest, ensure_ascii=False)},
    )
    assert response.status_code == 200, response.content
    state = response.json()
    batch = client.post(
        reverse("upload-photo-batch", args=[state["upload_id"]]),
        data={"batch_no": "0", "upload_token": state["upload_token"], "manifest_digest": state["photo_manifest_digest"], "photos": [photo_file(name + ".jpg")]},
    )
    assert batch.status_code == 200, batch.content
    session = UploadSession.objects.get(pk=state["upload_id"])
    session.status = "finalizing"
    session.save(update_fields=["status", "updated_at"])
    return session, state


def test_finalizing_partial_submission_never_returns_success(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    session, state = _finalizing_partial_session(Client(), "finalizing仅学生")
    with session.roster_file.open("rb") as source:
        records = services.parse_roster(SimpleUploadedFile("名单.xlsx", source.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    school_year, class_group = services._class_from_record(records[0])
    partial = Submission.objects.create(
        access_token_hash=hashlib.sha256(services._session_result_token(session.id).encode()).hexdigest(),
        department=class_group.department, school_year=school_year, grade=class_group.grade,
        class_group=class_group, source_filename="半成品.xlsx",
    )
    Student.objects.create(
        submission=partial, name=records[0]["姓名"], gender="男", department=class_group.department,
        school_year=school_year, grade=class_group.grade, class_name=records[0]["班级"], study_type="走读",
    )
    with pytest.raises(ValidationError, match="正在完成"):
        services.finalize_upload_session(session_id=session.id, token=state["upload_token"], manifest_digest=session.photo_manifest_digest)
    assert UploadSession.objects.get(pk=session.id).status == "finalizing"
    assert Submission.objects.filter(pk=partial.id).exists()


def test_finalizing_submission_with_partial_photo_never_returns_success(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    session, state = _finalizing_partial_session(Client(), "finalizing部分照片")
    with session.roster_file.open("rb") as source:
        records = services.parse_roster(SimpleUploadedFile("名单.xlsx", source.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    school_year, class_group = services._class_from_record(records[0])
    partial = Submission.objects.create(
        access_token_hash=hashlib.sha256(services._session_result_token(session.id).encode()).hexdigest(),
        department=class_group.department, school_year=school_year, grade=class_group.grade,
        class_group=class_group, source_filename="半成品照片.xlsx",
    )
    student = Student.objects.create(
        submission=partial, name=records[0]["姓名"], gender="男", department=class_group.department,
        school_year=school_year, grade=class_group.grade, class_name=records[0]["班级"], study_type="走读",
    )
    staged = StagedUpload.objects.filter(batch__session=session, batch__kind="photo").get()
    StudentPhoto.objects.create(student=student, submission=partial, original_name=staged.original_name, file=staged.file.name, content_type=staged.content_type, size=staged.size, sha256=staged.sha256)
    with pytest.raises(ValidationError, match="正在完成"):
        services.finalize_upload_session(session_id=session.id, token=state["upload_token"], manifest_digest=session.photo_manifest_digest)
    assert UploadSession.objects.get(pk=session.id).status == "finalizing"
    assert StudentPhoto.objects.filter(submission=partial).count() == 1


def test_finalizing_stale_partial_submission_recovers_without_duplicate(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    session, state = _finalizing_partial_session(Client(), "finalizing过期")
    with session.roster_file.open("rb") as source:
        records = services.parse_roster(SimpleUploadedFile("名单.xlsx", source.read(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    school_year, class_group = services._class_from_record(records[0])
    partial = Submission.objects.create(
        access_token_hash=hashlib.sha256(services._session_result_token(session.id).encode()).hexdigest(),
        department=class_group.department, school_year=school_year, grade=class_group.grade,
        class_group=class_group, source_filename="半成品.xlsx",
    )
    session.updated_at = timezone.now() - timedelta(seconds=3600)
    UploadSession.objects.filter(pk=session.pk).update(status="finalizing", updated_at=session.updated_at)
    recovered, _ = services.finalize_upload_session(session_id=session.id, token=state["upload_token"], manifest_digest=session.photo_manifest_digest)
    assert recovered.id != partial.id
    assert not Submission.objects.filter(pk=partial.id).exists()
    assert recovered.students.count() == 1
    assert recovered.photos.count() == 1
    assert Submission.objects.filter(access_token_hash=hashlib.sha256(services._session_result_token(session.id).encode()).hexdigest()).count() == 1


def test_two_complete_requests_share_one_result_or_retry_safely(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client = Client()
    photo = photo_file("双完成.jpg")
    photo_data = photo.read()
    photo.seek(0)
    response = client.post(reverse("upload-start"), data={"roster": roster_file([["双完成", "女", "高中部", "2026", "高一", "F6-双", "住宿", ""]]), "expected_photo_count": "1", "photo_manifest": json.dumps([{"name": photo.name, "size": len(photo_data), "sha256": hashlib.sha256(photo_data).hexdigest()}])})
    assert response.status_code == 200, response.content
    state = response.json()
    photo.seek(0)
    assert client.post(reverse("upload-photo-batch", args=[state["upload_id"]]), data={"batch_no": "0", "upload_token": state["upload_token"], "manifest_digest": state["photo_manifest_digest"], "photos": [photo_file("双完成.jpg")] }).status_code == 200

    def complete_once():
        from django.db import close_old_connections
        close_old_connections()
        try:
            result = services.finalize_upload_session(session_id=state["upload_id"], token=state["upload_token"], manifest_digest=state["photo_manifest_digest"])
            return ("ok", str(result[0].id), result[1])
        except ValidationError as exc:
            return ("retry", str(exc), "")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: complete_once(), range(2)))
    assert {item[0] for item in results} <= {"ok", "retry"}
    assert any(item[0] == "ok" for item in results)
    assert Submission.objects.count() == 1
    assert UploadSession.objects.get(pk=state["upload_id"]).status == "completed"


def test_export_source_read_failure_has_no_partial_photo_member(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    submission = complete_submission([["导出失败", "男", "高中部", "2026", "高一", "F7", "走读", ""]], [photo_file("导出失败.jpg")])
    admin = User.objects.create(username="r2-f7")
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    original_stage = services._stage_export_file

    def fail_after_first_chunk(file_field, **kwargs):
        staged = original_stage(file_field, **kwargs)
        staged.write_bytes(staged.read_bytes()[:1])
        staged.unlink(missing_ok=True)
        raise services.ExportFileError("模拟源文件在首块后读取失败")

    monkeypatch.setattr(services, "_stage_export_file", fail_after_first_chunk)
    temp_path, _, _ = export_students_to_tempfile({"school_year__name": "2026"}, actor=admin)
    try:
        with zipfile.ZipFile(temp_path) as archive:
            assert archive.testzip() is None
            assert not [name for name in archive.namelist() if name.lower().endswith(".jpg")]
            stats = next(name for name in archive.namelist() if name.endswith("学生统计表.xlsx"))
            book = load_workbook(io.BytesIO(archive.read(stats)), data_only=True)
            assert book["统计汇总"]["B8"].value == 0
            assert book["统计汇总"]["B9"].value == 1
            assert any("读取失败" in str(row[2].value or "") for row in book["异常记录"].iter_rows(min_row=2))
    finally:
        temp_path.unlink(missing_ok=True)


@pytest.mark.parametrize("target", ["roster", "document"])
def test_export_roster_and_document_read_failure_has_no_partial_member(settings, tmp_path, monkeypatch, target):
    settings.MEDIA_ROOT = tmp_path
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    documents = [document_file("中途失败.pdf")] if target == "document" else []
    submission = complete_submission([["名单文档失败", "女", "高中部", "2026", "高一", "F8", "走读", ""]], [photo_file("名单文档失败.jpg")], documents)
    admin = User.objects.create(username=f"r2-f8-{target}")
    review_submission_version(submission_id=submission.id, action="approved", actor=admin)
    target_name = submission.roster_file.name if target == "roster" else submission.documents.get().file.name
    original_stage = services._stage_export_file

    def fail_target(file_field, **kwargs):
        staged = original_stage(file_field, **kwargs)
        if file_field.name == target_name:
            staged.write_bytes(staged.read_bytes()[:1])
            staged.unlink(missing_ok=True)
            raise services.ExportFileError(f"模拟{target}在首块后读取失败")
        return staged

    monkeypatch.setattr(services, "_stage_export_file", fail_target)
    temp_path, _, _ = export_students_to_tempfile({"school_year__name": "2026"}, actor=admin)
    try:
        with zipfile.ZipFile(temp_path) as archive:
            assert archive.testzip() is None
            assert not any(f"/{'学生名单' if target == 'roster' else '相关文档'}/" in name for name in archive.namelist())
            stats = next(name for name in archive.namelist() if name.endswith("学生统计表.xlsx"))
            book = load_workbook(io.BytesIO(archive.read(stats)), data_only=True)
            assert any(f"模拟{target}" in str(row[2].value or "") for row in book["异常记录"].iter_rows(min_row=2))
    finally:
        temp_path.unlink(missing_ok=True)


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
