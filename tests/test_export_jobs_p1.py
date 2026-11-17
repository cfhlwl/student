import io
import os
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from collector.backup import clear_collection_safely
from collector.export_jobs import claim_next_export_job, cleanup_export_jobs, enqueue_export_job, process_export_job
from collector.maintenance import write_state
from collector.models import AuditLog, ClassGroup, ExportJob, SchoolYear, Student, Submission


def approved_student(name="张三"):
    year = SchoolYear.objects.create(name="2026")
    class_group = ClassGroup.objects.create(department="high", school_year=year, grade="高一", name="1")
    submission = Submission.objects.create(
        access_token_hash=(name.encode("utf-8").hex() + "0" * 64)[:64],
        department="high",
        school_year=year,
        grade="高一",
        class_group=class_group,
        status="approved",
        source_filename="名单.xlsx",
    )
    return Student.objects.create(
        submission=submission,
        name=name,
        gender="男",
        department="high",
        school_year=year,
        grade="高一",
        class_name="1",
        study_type="走读",
    )


@pytest.mark.django_db
def test_export_snapshot_changes_after_new_approved_student(settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    first_student = approved_student()
    first = enqueue_export_job({})
    process_export_job(claim_next_export_job())
    first.refresh_from_db()
    assert first.status == "completed"
    with zipfile.ZipFile(first.file_path) as archive:
        assert archive.testzip() is None

    Student.objects.create(
        submission=first_student.submission,
        name="李四",
        gender="女",
        department="high",
        school_year=first_student.school_year,
        grade="高一",
        class_name="1",
        study_type="住宿",
    )
    second = enqueue_export_job({})
    assert second.pk != first.pk
    process_export_job(claim_next_export_job())
    second.refresh_from_db()
    assert second.status == "completed"
    assert second.student_count == 2
    assert Path(second.file_path).is_file()


@pytest.mark.django_db
def test_completed_export_can_be_repeated_and_old_zip_is_retained(settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    approved_student()
    first = enqueue_export_job({})
    process_export_job(claim_next_export_job())
    first.refresh_from_db()
    second = enqueue_export_job({})
    assert second.pk != first.pk
    assert Path(first.file_path).is_file()
    process_export_job(claim_next_export_job())
    second.refresh_from_db()
    assert second.status == "completed"
    assert Path(second.file_path).is_file()
    assert ExportJob.objects.filter(status="completed").count() == 2


@pytest.mark.django_db
def test_async_export_tolerates_missing_original_roster(settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    settings.MEDIA_ROOT = tmp_path / "media"
    write_state("open")
    student = approved_student()
    student.submission.roster_file.name = "submissions/missing/名单.xlsx"
    student.submission.save(update_fields=["roster_file"])
    job = enqueue_export_job({})
    process_export_job(claim_next_export_job())
    job.refresh_from_db()
    assert job.status == "completed"
    assert stat.S_IMODE(Path(job.file_path).stat().st_mode) & 0o004
    with zipfile.ZipFile(job.file_path) as archive:
        assert archive.testzip() is None
        stats_name = next(name for name in archive.namelist() if name.endswith("学生统计表.xlsx"))
        workbook = load_workbook(io.BytesIO(archive.read(stats_name)), read_only=True)
        anomaly_rows = list(workbook["异常记录"].values)
    assert any("文件缺失" in str(cell) for row in anomaly_rows for cell in row)


@pytest.mark.django_db
def test_stale_export_is_terminal_and_new_export_can_succeed(settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    first_student = approved_student()
    old_job = enqueue_export_job({})
    old_claim = claim_next_export_job()
    Student.objects.create(
        submission=first_student.submission,
        name="资料更新后的学生",
        gender="女",
        department="high",
        school_year=first_student.school_year,
        grade="高一",
        class_name="1",
        study_type="住宿",
    )
    process_export_job(old_claim)
    old_job.refresh_from_db()
    assert old_job.status == "stale"
    assert old_job.file_path == ""

    admin = User.objects.create_user(username="stale-export-admin", password="strong-password-123", is_staff=True)
    client = Client()
    client.force_login(admin)
    assert client.get(reverse("admin-export-download", args=[old_job.pk]), HTTP_HOST="localhost").status_code == 404
    page = client.get(reverse("admin-export-status", args=[old_job.pk]), HTTP_HOST="localhost")
    content = page.content.decode()
    assert "学生资料已更新，请重新导出" in content
    assert "data.status === 'stale'" in content

    new_job = enqueue_export_job({})
    assert new_job.pk != old_job.pk
    process_export_job(claim_next_export_job())
    new_job.refresh_from_db()
    assert new_job.status == "completed"
    assert new_job.student_count == 2


@pytest.mark.django_db
def test_stale_worker_cannot_publish_after_lease_requeue(settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    settings.EXPORT_JOB_TIMEOUT_SECONDS = 60
    write_state("open")
    approved_student()
    job = enqueue_export_job({})
    old_claim = claim_next_export_job()
    ExportJob.objects.filter(pk=job.pk).update(started_at=timezone.now() - timedelta(seconds=61))
    cleanup_export_jobs()
    job.refresh_from_db()
    assert job.status == "queued"
    new_claim = claim_next_export_job()
    assert new_claim.lease_token != old_claim.lease_token
    process_export_job(old_claim)
    job.refresh_from_db()
    assert job.status == "running"
    process_export_job(new_claim)
    job.refresh_from_db()
    assert job.status == "completed"
    assert Path(job.file_path).name.endswith(f"-{new_claim.lease_token}.zip")


@pytest.mark.django_db(transaction=True)
def test_two_sqlite_workers_can_claim_only_once(settings, tmp_path):
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    ExportJob.objects.create(fingerprint="c" * 64, data_snapshot="d" * 64)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _index: claim_next_export_job(), range(2)))
    assert sum(claim is not None for claim in claims) == 1


@pytest.mark.django_db
def test_clear_revokes_export_jobs_and_removes_archives(settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    archive_dir = Path(settings.EXPORT_TEMP_DIR)
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "export-job-1-old.zip"
    archive.write_bytes(b"PK\x03\x04synthetic")
    job = ExportJob.objects.create(fingerprint="a" * 64, data_snapshot="b" * 64, file_path=str(archive), status="completed")
    backup = tmp_path / "backup.zip"
    clear_collection_safely(backup, media_root=settings.MEDIA_ROOT)
    assert not ExportJob.objects.filter(pk=job.pk).exists()
    assert not archive.exists()
    assert not list(archive_dir.glob("*.zip"))


@pytest.mark.django_db
def test_admin_export_download_center_deletes_only_selected_zip(client, settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    archive_dir = Path(settings.EXPORT_TEMP_DIR)
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "export-job-1-retained.zip"
    archive.write_bytes(b"synthetic zip")
    job = ExportJob.objects.create(
        fingerprint="d" * 64,
        data_snapshot="e" * 64,
        file_path=str(archive),
        status="completed",
        student_count=1,
        total_count=1,
        processed_count=1,
    )
    admin = User.objects.create_user(username="download-center-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    page = client.get(reverse("admin-exports"))
    assert page.status_code == 200
    assert f"#{job.pk}" in page.content.decode()
    response = client.post(reverse("admin-export-delete", args=[job.pk]))
    assert response.status_code == 302
    assert not ExportJob.objects.filter(pk=job.pk).exists()
    assert not archive.exists()
    assert AuditLog.objects.filter(action="delete_export", target_id=str(job.pk)).exists()


@pytest.mark.django_db
def test_cleanup_keeps_completed_zip_until_admin_deletes_it(settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    archive_dir = Path(settings.EXPORT_TEMP_DIR)
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "export-job-retained.zip"
    archive.write_bytes(b"synthetic zip")
    job = ExportJob.objects.create(
        fingerprint="e" * 64,
        data_snapshot="f" * 64,
        file_path=str(archive),
        status="completed",
        expires_at=timezone.now() - timedelta(days=1),
    )
    cleanup_export_jobs()
    job.refresh_from_db()
    assert job.status == "completed"
    assert Path(job.file_path).is_file()


@pytest.mark.django_db
def test_disk_full_fails_new_job_without_touching_retained_zip(monkeypatch, settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    approved_student()
    old_dir = Path(settings.EXPORT_TEMP_DIR)
    old_dir.mkdir(parents=True)
    old_archive = old_dir / "export-job-old-retained.zip"
    old_archive.write_bytes(b"old archive")
    old_job = ExportJob.objects.create(
        fingerprint="1" * 64,
        data_snapshot="2" * 64,
        file_path=str(old_archive),
        status="completed",
    )
    new_job = enqueue_export_job({})
    claimed = claim_next_export_job()

    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("collector.export_jobs.export_students_to_tempfile", no_space)
    process_export_job(claimed)
    new_job.refresh_from_db()
    old_job.refresh_from_db()
    assert new_job.status == "failed"
    assert "No space left on device" in new_job.error
    assert old_job.status == "completed"
    assert old_archive.read_bytes() == b"old archive"


@pytest.mark.django_db
def test_delete_rejects_path_outside_export_root_and_running_job(client, settings, tmp_path):
    settings.EXPORT_TEMP_DIR = tmp_path / "exports"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "state.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "state.lock"
    write_state("open")
    export_dir = Path(settings.EXPORT_TEMP_DIR)
    export_dir.mkdir(parents=True)
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"protected")
    completed = ExportJob.objects.create(
        fingerprint="3" * 64,
        data_snapshot="4" * 64,
        file_path=str(export_dir / ".." / "outside.zip"),
        status="completed",
    )
    running_archive = export_dir / "running.zip"
    running_archive.write_bytes(b"running")
    running = ExportJob.objects.create(
        fingerprint="5" * 64,
        data_snapshot="6" * 64,
        file_path=str(running_archive),
        status="running",
    )
    admin = User.objects.create_user(username="delete-safety-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    assert client.post(reverse("admin-export-delete", args=[completed.pk])).status_code == 302
    assert ExportJob.objects.filter(pk=completed.pk).exists()
    assert outside.read_bytes() == b"protected"
    assert client.post(reverse("admin-export-delete", args=[running.pk])).status_code == 302
    assert ExportJob.objects.filter(pk=running.pk).exists()
    assert running_archive.read_bytes() == b"running"

    symlink = export_dir / "linked.zip"
    try:
        os.symlink(outside, symlink)
    except (OSError, NotImplementedError):
        symlink = None
    if symlink is not None:
        linked = ExportJob.objects.create(
            fingerprint="7" * 64,
            data_snapshot="8" * 64,
            file_path=str(symlink),
            status="completed",
        )
        assert client.post(reverse("admin-export-delete", args=[linked.pk])).status_code == 302
        assert ExportJob.objects.filter(pk=linked.pk).exists()
        assert outside.read_bytes() == b"protected"
        assert symlink.is_symlink()
