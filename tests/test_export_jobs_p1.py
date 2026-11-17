import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from collector.backup import clear_collection_safely
from collector.export_jobs import claim_next_export_job, cleanup_export_jobs, enqueue_export_job, process_export_job
from collector.maintenance import write_state
from collector.models import ClassGroup, ExportJob, SchoolYear, Student, Submission


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
