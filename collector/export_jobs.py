import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AppSetting, ExportJob, NamingRule
from .services import export_students_to_tempfile, validate_naming_rule


EXPORT_FILE_NAME = "学生资料导出.zip"


def _export_options():
    rule = validate_naming_rule(NamingRule.current())
    layout = AppSetting.get_value("export_layout", "by_class")
    if layout not in {"by_class", "by_study_type"}:
        layout = "by_class"
    return {
        "mode": rule.mode,
        "template": rule.template,
        "male_code": rule.male_code,
        "female_code": rule.female_code,
        "separator": rule.separator,
        "layout": layout,
    }


def build_export_fingerprint(filters):
    payload = {"filters": filters, "options": _export_options()}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def enqueue_export_job(filters, actor=None, ip_address=None):
    fingerprint = build_export_fingerprint(filters)
    now = timezone.now()
    retention = timedelta(hours=max(1, settings.EXPORT_RETENTION_HOURS))
    with transaction.atomic():
        job = ExportJob.objects.filter(fingerprint=fingerprint).first()
        if job and job.status in {"queued", "running"}:
            return job
        if job and job.status == "completed" and job.file_path and Path(job.file_path).is_file():
            return job
        if job:
            job.status = "queued"
            job.error = ""
            job.file_path = ""
            job.student_count = 0
            job.preview = []
            job.started_at = None
            job.completed_at = None
            job.expires_at = None
            job.requested_by = actor
            job.requested_ip = ip_address
            job.created_at = now
            job.save()
            return job
        try:
            return ExportJob.objects.create(
                fingerprint=fingerprint,
                filters=filters,
                requested_by=actor,
                requested_ip=ip_address,
                expires_at=now + retention,
            )
        except IntegrityError:
            return ExportJob.objects.get(fingerprint=fingerprint)


def claim_next_export_job():
    with transaction.atomic():
        job = ExportJob.objects.filter(status="queued").order_by("created_at", "id").first()
        if job is None:
            return None
        job.status = "running"
        job.started_at = timezone.now()
        job.error = ""
        job.save(update_fields=["status", "started_at", "error", "updated_at"])
        return job


def process_export_job(job):
    temp_path = None
    final_path = Path(settings.EXPORT_TEMP_DIR) / f"export-job-{job.pk}.zip"
    try:
        temp_path, count, preview = export_students_to_tempfile(
            job.filters,
            actor=job.requested_by,
            ip_address=job.requested_ip,
        )
        os.replace(temp_path, final_path)
        temp_path = None
        expires_at = timezone.now() + timedelta(hours=max(1, settings.EXPORT_RETENTION_HOURS))
        ExportJob.objects.filter(pk=job.pk).update(
            status="completed",
            file_path=str(final_path),
            student_count=count,
            preview=preview,
            completed_at=timezone.now(),
            expires_at=expires_at,
            error="",
        )
    except Exception as exc:
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        ExportJob.objects.filter(pk=job.pk).update(status="failed", error=str(exc)[:2000], completed_at=timezone.now())


def cleanup_export_jobs():
    now = timezone.now()
    stale_before = now - timedelta(seconds=max(60, settings.EXPORT_JOB_TIMEOUT_SECONDS))
    ExportJob.objects.filter(status="running", started_at__lt=stale_before).update(
        status="queued", started_at=None, error="导出 worker 中断，已重新排队。"
    )
    expired = ExportJob.objects.filter(status="completed", expires_at__lt=now)
    for job in expired.iterator():
        if job.file_path:
            Path(job.file_path).unlink(missing_ok=True)
        job.status = "expired"
        job.file_path = ""
        job.save(update_fields=["status", "file_path", "updated_at"])
    export_dir = Path(settings.EXPORT_TEMP_DIR)
    if not export_dir.is_dir():
        return
    orphan_before = now.timestamp() - max(3600, settings.EXPORT_JOB_TIMEOUT_SECONDS)
    for pattern in ("student-export-*.zip", "student-export-source-*"):
        for path in export_dir.glob(pattern):
            try:
                if path.stat().st_mtime < orphan_before:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
