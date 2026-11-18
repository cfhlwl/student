import hashlib
import json
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
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
DELETE_QUARANTINE_RE = re.compile(r"^\.student-delete-(?P<job_id>[1-9][0-9]*)-(?P<token>[0-9a-f]{24})\.zip$")


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


def _selected_students(filters):
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
    return students


def build_export_snapshot(filters):
    """Digest every database row that can affect the resulting archive."""
    students = _selected_students(filters)
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


def count_export_students(filters):
    return len(_selected_students(filters))


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
        total_count = count_export_students(filters)
        with transaction.atomic():
            active_job = ExportJob.objects.filter(
                fingerprint=fingerprint,
                status__in=["queued", "running"],
            ).order_by("created_at", "id").first()
            if active_job:
                return active_job
            active_count = ExportJob.objects.filter(status__in=["queued", "running"]).count()
            if active_count >= settings.EXPORT_MAX_ACTIVE_JOBS:
                raise ExportQueueFull("导出任务较多，请等待当前任务完成后再试。")
            return ExportJob.objects.create(
                fingerprint=fingerprint,
                filters=filters,
                data_snapshot=snapshot,
                requested_by=actor,
                requested_ip=ip_address,
                file_name=export_file_name(filters),
                total_count=total_count,
                processed_count=0,
                expires_at=None,
            )


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


def _update_export_progress(job, token, processed_count, total_count):
    with collection_write_gate():
        updated = ExportJob.objects.filter(
            pk=job.pk,
            status="running",
            lease_token=token,
            attempt=job.attempt,
        ).update(total_count=total_count, processed_count=processed_count)
    if updated != 1:
        raise ExportLeaseLost("导出任务租约已失效")


def process_export_job(job):
    token = job.lease_token
    final_path = Path(settings.EXPORT_TEMP_DIR) / f"export-job-{job.pk}-{token}.zip"
    temp_path = None
    export_metrics = {}
    try:
        temp_path, count, preview = export_students_to_tempfile(
            job.filters,
            actor=job.requested_by,
            ip_address=job.requested_ip,
            progress_callback=lambda processed, total: _update_export_progress(job, token, processed, total),
            export_metrics=export_metrics,
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
            updated = ExportJob.objects.filter(
                pk=job.pk, status="running", lease_token=token, attempt=job.attempt,
            ).update(
                status="completed",
                file_path=str(final_path),
                student_count=count,
                exported_photo_count=export_metrics.get("exported_photo_count"),
                fallback_original_count=export_metrics.get("fallback_original_count"),
                source_unavailable_count=export_metrics.get("source_unavailable_count"),
                rejection_reasons=export_metrics.get("rejection_reasons", {}),
                preview=preview,
                completed_at=timezone.now(),
                expires_at=None,
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


def _safe_delete_quarantine_paths(export_dir):
    """Recover or finish quarantined deletes after a web-process crash.

    The caller must hold collection_write_gate(). Quarantine files are only
    considered when they are direct, non-symlink children of EXPORT_TEMP_DIR.
    Recovery uses an exclusive hard-link creation so an existing ZIP can never
    be overwritten. A second cleanup pass completes a recovery interrupted
    after the link was created but before the quarantine entry was removed.
    """
    if not export_dir.is_dir() or export_dir.is_symlink():
        return
    export_root = export_dir.resolve()
    quarantines = {}
    for candidate in export_dir.glob(".student-delete-*.zip"):
        try:
            match = DELETE_QUARANTINE_RE.fullmatch(candidate.name)
            if not match or candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.parent.is_symlink() or candidate.parent.resolve() != export_root:
                continue
            quarantines.setdefault(int(match.group("job_id")), []).append(candidate)
        except OSError:
            continue
    if not quarantines:
        return

    jobs = {
        job.pk: job
        for job in ExportJob.objects.filter(pk__in=quarantines).only("pk", "file_path")
    }
    for job_id, paths in quarantines.items():
        job = jobs.get(job_id)
        if job is None:
            # The transaction committed and removed the task. The quarantine
            # is now an orphan, but still remove only the exact safe names we
            # discovered above.
            for quarantine in paths:
                try:
                    if not quarantine.is_symlink() and quarantine.is_file():
                        quarantine.unlink(missing_ok=True)
                except OSError:
                    continue
            continue

        try:
            source = Path(job.file_path)
            if not job.file_path or source.is_symlink() or source.parent.is_symlink():
                continue
            if source.resolve().parent != export_root:
                continue
            if source.exists():
                # The source was already restored before a previous crash;
                # remove only the redundant quarantine entries.
                for quarantine in paths:
                    try:
                        if not quarantine.is_symlink() and quarantine.is_file():
                            quarantine.unlink(missing_ok=True)
                    except OSError:
                        continue
                continue
            if len(paths) != 1:
                # Ambiguous state is retained for manual review rather than
                # guessing which ZIP belongs to the task.
                continue
            quarantine = paths[0]
            if quarantine.is_symlink() or not quarantine.is_file():
                continue
            # os.link fails if source appeared meanwhile; unlike os.replace it
            # cannot overwrite an existing ZIP or follow a destination link.
            os.link(quarantine, source)
            try:
                quarantine.unlink(missing_ok=True)
            except OSError:
                # The source is already safely restored. A later idempotent
                # cleanup pass will remove this redundant directory entry.
                continue
        except (OSError, ValueError):
            continue


def cleanup_export_jobs():
    """Requeue only stale leases and delete only their own immutable files."""
    try:
        with collection_write_gate():
            now = timezone.now()
            stale_before = now - timedelta(seconds=max(60, settings.EXPORT_JOB_TIMEOUT_SECONDS))
            ExportJob.objects.filter(status="running", started_at__lt=stale_before).update(
                status="queued", started_at=None, error="导出 worker 中断，已重新排队。", lease_token=""
            )
            # Completed archives are retained until an administrator explicitly
            # deletes the task from the export download center.  The cleanup
            # loop only handles abandoned workers and unreferenced temp files.
            export_dir = Path(settings.EXPORT_TEMP_DIR)
            if not export_dir.is_dir():
                return
            _safe_delete_quarantine_paths(export_dir)
            orphan_before = now.timestamp() - max(3600, settings.EXPORT_JOB_TIMEOUT_SECONDS)
            handoff_before = now.timestamp() - max(60, settings.EXPORT_DOWNLOAD_HANDOFF_GRACE_SECONDS)
            for path in export_dir.glob(".student-download-handoff-*.zip"):
                try:
                    if path.stat().st_mtime < handoff_before:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue
            for pattern in ("student-export-*.zip", "student-export-source-*"):
                for path in export_dir.glob(pattern):
                    try:
                        if path.stat().st_mtime < orphan_before:
                            path.unlink(missing_ok=True)
                    except OSError:
                        continue
    except MaintenanceError:
        return
