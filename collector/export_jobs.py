import hashlib
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .maintenance import MaintenanceError, collection_write_gate
from .models import AppSetting, ExportJob, NamingRule, Student, StudentPhoto, Submission, SubmissionDocument
from .face_export import DEVICE_EXPORT_PROFILE_VERSION, DEVICE_MAX_BYTES, DEVICE_FACE_HEIGHT_RATIO, DEVICE_FACE_MIN_PIXELS
from .services import EXPORT_MODE_FACE_DEVICE, EXPORT_MODE_ORIGINAL, export_students_to_tempfile, validate_naming_rule


EXPORT_FILE_NAME = "学生资料导出.zip"
# The archive is served by nginx through an internal X-Accel-Redirect. The
# worker runs as root while nginx workers run as uid 101, so a mkstemp-created
# 0600 archive cannot be read by nginx. Keep the URL protected by nginx's
# `internal` location, but make the completed file readable by its consumer.
EXPORT_FILE_MODE = 0o644


class ExportQueueFull(Exception):
    pass


class ExportLeaseLost(Exception):
    pass


def _export_options():
    rule = validate_naming_rule(NamingRule.current())
    layout = AppSetting.get_value("export_layout", "by_class")
    if layout not in {"by_class", "by_study_type"}:
        layout = "by_class"
    options = {
        "mode": rule.mode,
        "template": rule.template,
        "male_code": rule.male_code,
        "female_code": rule.female_code,
        "separator": rule.separator,
        "layout": layout,
    }
    return options


def export_file_name(filters):
    return "人脸识别设备资料.zip" if filters.get("export_mode") == EXPORT_MODE_FACE_DEVICE else EXPORT_FILE_NAME


def build_export_snapshot(filters):
    """Digest every database row that can affect the resulting archive."""
    qs = Student.objects.select_related("submission", "school_year").filter(submission__status="approved")
    for key in ["school_year__name", "department", "grade", "class_name", "gender", "study_type"]:
        if filters.get(key):
            qs = qs.filter(**{key: filters[key]})
    students = list(qs.order_by("department", "grade", "class_name", "name", "id"))
    photos = {
        row.student_id: row
        for row in StudentPhoto.objects.filter(student_id__in=[student.id for student in students])
    }
    if filters.get("has_photo") == "yes":
        students = [student for student in students if student.match_status == "matched" and student.id in photos]
    elif filters.get("has_photo") == "no":
        students = [student for student in students if not (student.match_status == "matched" and student.id in photos)]
    submission_ids = {student.submission_id for student in students}
    submissions = Submission.objects.filter(id__in=submission_ids).order_by("id")
    documents = SubmissionDocument.objects.filter(submission_id__in=submission_ids).order_by("id")

    def stored_size(field):
        try:
            return field.size
        except (FileNotFoundError, OSError, ValueError):
            # The export service deliberately handles missing managed files as
            # archive anomalies. Snapshotting must not turn that compatibility
            # path into a synchronous enqueue failure.
            return None

    payload = {
        "students": [
            {
                "id": student.id,
                "submission_id": str(student.submission_id),
                "student_no": student.student_no,
                "name": student.name,
                "gender": student.gender,
                "department": student.department,
                "school_year_id": student.school_year_id,
                "school_year_name": student.school_year.name,
                "grade": student.grade,
                "class_name": student.class_name,
                "study_type": student.study_type,
                "remark": student.remark,
                "match_status": student.match_status,
                "photo": None if student.id not in photos else {
                    "id": photos[student.id].id,
                    "name": photos[student.id].original_name,
                    "sha256": photos[student.id].sha256,
                    "size": photos[student.id].size,
                    "file": photos[student.id].file.name,
                },
            }
            for student in students
        ],
        "submissions": [
            {
                "id": str(row.id),
                "source_filename": row.source_filename,
                "roster_file": row.roster_file.name if row.roster_file else "",
                "roster_size": stored_size(row.roster_file) if row.roster_file else 0,
            }
            for row in submissions
        ],
        "documents": [
            {
                "id": row.id,
                "submission_id": row.submission_id,
                "name": row.original_name,
                "size": row.size,
                "content_type": row.content_type,
                "file": row.file.name,
            }
            for row in documents
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_export_fingerprint(filters, snapshot=None):
    snapshot = snapshot or build_export_snapshot(filters)
    options = _export_options()
    if filters.get("export_mode") == EXPORT_MODE_FACE_DEVICE:
        options["device_profile"] = {
            "version": DEVICE_EXPORT_PROFILE_VERSION,
            "max_bytes": DEVICE_MAX_BYTES,
            "min_face_pixels": DEVICE_FACE_MIN_PIXELS,
            "min_face_height_ratio": DEVICE_FACE_HEIGHT_RATIO,
        }
    payload = {"filters": filters, "options": options, "data_snapshot": snapshot}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), snapshot


def enqueue_export_job(filters, actor=None, ip_address=None):
    filters = dict(filters)
    export_mode = filters.get("export_mode", EXPORT_MODE_ORIGINAL)
    if export_mode not in {EXPORT_MODE_ORIGINAL, EXPORT_MODE_FACE_DEVICE}:
        raise ValueError("导出模式不正确")
    if export_mode == EXPORT_MODE_ORIGINAL:
        filters.pop("export_mode", None)
    else:
        filters["export_mode"] = export_mode
    with collection_write_gate():
        fingerprint, snapshot = build_export_fingerprint(filters)
        now = timezone.now()
        retention = timedelta(hours=max(1, settings.EXPORT_RETENTION_HOURS))
        with transaction.atomic():
            job = ExportJob.objects.filter(fingerprint=fingerprint).first()
            if job and job.status in {"queued", "running"}:
                return job
            if job and job.status == "completed" and job.expires_at and job.expires_at > now and job.file_path and Path(job.file_path).is_file():
                return job
            if job:
                previous_file = Path(job.file_path) if job.file_path else None
                job.status = "queued"
                job.data_snapshot = snapshot
                job.error = ""
                job.file_path = ""
                job.student_count = 0
                job.preview = []
                job.file_name = export_file_name(filters)
                job.started_at = None
                job.completed_at = None
                job.expires_at = None
                job.lease_token = ""
                job.requested_by = actor
                job.requested_ip = ip_address
                job.created_at = now
                job.save()
                if previous_file:
                    previous_file.unlink(missing_ok=True)
                return job
            active_count = ExportJob.objects.filter(status__in=["queued", "running"]).count()
            if active_count >= settings.EXPORT_MAX_ACTIVE_JOBS:
                raise ExportQueueFull("导出任务较多，请等待当前任务完成后再试。")
            try:
                return ExportJob.objects.create(
                    fingerprint=fingerprint,
                    filters=filters,
                    data_snapshot=snapshot,
                    requested_by=actor,
                    requested_ip=ip_address,
                    file_name=export_file_name(filters),
                    expires_at=now + retention,
                )
            except IntegrityError:
                return ExportJob.objects.get(fingerprint=fingerprint)


def claim_next_export_job():
    """Claim with a CAS and lease token; select_for_update is insufficient on SQLite."""
    try:
        with collection_write_gate():
            for _ in range(3):
                candidate = ExportJob.objects.filter(status="queued").order_by("created_at", "id").first()
                if candidate is None:
                    return None
                token = secrets.token_hex(24)
                updated = ExportJob.objects.filter(pk=candidate.pk, status="queued").update(
                    status="running",
                    started_at=timezone.now(),
                    lease_token=token,
                    attempt=F("attempt") + 1,
                    error="",
                )
                if updated == 1:
                    candidate.refresh_from_db()
                    return candidate
            return None
    except MaintenanceError:
        return None


def _mark_stale(job, token, message):
    ExportJob.objects.filter(pk=job.pk, status="running", lease_token=token, attempt=job.attempt).update(
        status="stale", error=message, completed_at=timezone.now(), file_path=""
    )


def _make_export_file_readable(path):
    """Publish an archive that nginx can read without exposing its URL."""
    os.chmod(path, EXPORT_FILE_MODE)


def process_export_job(job):
    token = job.lease_token
    final_path = Path(settings.EXPORT_TEMP_DIR) / f"export-job-{job.pk}-{token}.zip"
    temp_path = None
    try:
        temp_path, count, preview = export_students_to_tempfile(
            job.filters,
            actor=job.requested_by,
            ip_address=job.requested_ip,
        )
        with collection_write_gate():
            current = ExportJob.objects.filter(
                pk=job.pk, status="running", lease_token=token, attempt=job.attempt,
            ).first()
            if current is None:
                raise ExportLeaseLost("导出任务租约已失效")
            if build_export_snapshot(job.filters) != job.data_snapshot:
                _mark_stale(job, token, "生成期间资料发生变化，未发布旧快照。")
                Path(temp_path).unlink(missing_ok=True)
                temp_path = None
                return
            Path(settings.EXPORT_TEMP_DIR).mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, final_path)
            temp_path = None
            # Publish permissions before recording the completed path. If this
            # fails, the job is failed and no unreadable download link is saved.
            _make_export_file_readable(final_path)
            expires_at = timezone.now() + timedelta(hours=max(1, settings.EXPORT_RETENTION_HOURS))
            updated = ExportJob.objects.filter(
                pk=job.pk, status="running", lease_token=token, attempt=job.attempt,
            ).update(
                status="completed",
                file_path=str(final_path),
                student_count=count,
                preview=preview,
                completed_at=timezone.now(),
                expires_at=expires_at,
                error="",
            )
            if updated != 1:
                final_path.unlink(missing_ok=True)
                raise ExportLeaseLost("导出任务租约已失效")
    except Exception as exc:
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)
        if final_path.exists() and not ExportJob.objects.filter(pk=job.pk, status="completed", file_path=str(final_path)).exists():
            final_path.unlink(missing_ok=True)
        ExportJob.objects.filter(pk=job.pk, status="running", lease_token=token, attempt=job.attempt).update(
            status="failed", error=str(exc)[:2000], completed_at=timezone.now(), file_path=""
        )


def cleanup_export_jobs():
    """Requeue only stale leases and delete only their own immutable files."""
    try:
        with collection_write_gate():
            now = timezone.now()
            stale_before = now - timedelta(seconds=max(60, settings.EXPORT_JOB_TIMEOUT_SECONDS))
            ExportJob.objects.filter(status="running", started_at__lt=stale_before).update(
                status="queued", started_at=None, error="导出 worker 中断，已重新排队。", lease_token=""
            )
            expired = ExportJob.objects.filter(status="completed", expires_at__lt=now)
            for job in expired.iterator():
                path = Path(job.file_path) if job.file_path else None
                if path:
                    path.unlink(missing_ok=True)
                ExportJob.objects.filter(pk=job.pk, status="completed", file_path=job.file_path).update(
                    status="expired", file_path=""
                )
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
    except MaintenanceError:
        return
