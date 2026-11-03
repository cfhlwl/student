import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from pathlib import PurePosixPath

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from PIL import Image, UnidentifiedImageError

from .models import AppSetting, AuditLog, ClassGroup, NamingRule, SchoolYear, StagedUpload, Student, StudentPhoto, Submission, SubmissionDocument, UploadBatch, UploadSession

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
ALLOWED_TOKENS = {"{姓名}", "{性别编号}", "{学部}", "{班级}", "{就读类型}"}
FIELD_MAP = {
    "姓名": "name", "性别": "gender", "学部": "department", "学年": "school_year",
    "班级": "class_name", "就读类型": "study_type",
}
HEADERS = ["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"]
GRADE_BY_YEAR = {
    "2026": {"middle": "初一", "high": "高一"},
    "2025": {"middle": "初二", "high": "高二"},
    "2024": {"middle": "初三", "high": "高三"},
}
STUDY_TYPE_VALUES = ("走读", "半走读", "住宿")


@dataclass
class PreparedUpload:
    name: str
    original_name: str
    content_type: str
    size: int
    sha256: str
    source: object = None
    stored_name: str = ""


def _with_db_retry(operation, attempts=20):
    for attempt in range(attempts):
        try:
            return operation()
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _safe_upload_name(upload):
    return safe_filename(getattr(upload, "name", ""))


class ValidationError(Exception):
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


def safe_filename(value, fallback="未命名"):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip().strip(".")
    return (value or fallback)[:160]


def safe_excel_value(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    value = "" if value is None else str(value)
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def upload_sha256(upload):
    upload.seek(0)
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


def _normalize_photo_manifest(value, expected_count):
    if value in (None, "", []):
        if expected_count:
            return []
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError("照片清单格式不正确，请重新选择文件") from exc
    if not isinstance(value, list) or len(value) != expected_count:
        raise ValidationError("照片清单与照片数量不一致，请重新选择文件")
    manifest = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"第 {index} 个照片清单项格式不正确")
        original_name = str(item.get("name") or "")
        name = safe_filename(original_name)
        if not original_name or name != original_name:
            raise ValidationError(f"第 {index} 个照片文件名不安全")
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"第 {index} 个照片大小不正确") from exc
        sha256 = str(item.get("sha256") or "").lower()
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValidationError(f"第 {index} 个照片哈希不正确")
        manifest.append({"name": name, "size": size, "sha256": sha256})
    return manifest


def _manifest_json_digest(manifest):
    if not manifest:
        return ""
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _session_result_token(session_id):
    payload = f"student-collector-result:{session_id}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def image_bytes_info(upload):
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if upload.size > max_bytes:
        raise ValidationError(f"文件 {upload.name} 超过 {settings.MAX_UPLOAD_SIZE_MB} MB 限制")
    suffix = PurePosixPath(upload.name).suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValidationError(f"照片 {upload.name} 不是 JPG/PNG")
    upload.seek(0)
    try:
        with Image.open(upload) as image:
            image.verify()
            content_type = Image.MIME.get(image.format)
            if image.format not in {"JPEG", "PNG"}:
                raise ValidationError(f"照片 {upload.name} 的实际格式不受支持")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationError(f"照片 {upload.name} 无法读取") from exc
    upload.seek(0)
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return content_type, digest.hexdigest()


def document_info(upload):
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 4 * 1024 * 1024
    if upload.size > max_bytes:
        raise ValidationError(f"文档 {upload.name} 超过 {max_bytes // (1024 * 1024)} MB 限制")
    suffix = PurePosixPath(upload.name).suffix.lower()
    if suffix not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValidationError(f"文档 {upload.name} 类型不受支持")
    upload.seek(0)
    head = upload.read(8)
    upload.seek(0)
    signatures = {
        ".pdf": head.startswith(b"%PDF"),
        ".png": head.startswith(b"\x89PNG"),
        ".jpg": head.startswith(b"\xff\xd8\xff"),
        ".jpeg": head.startswith(b"\xff\xd8\xff"),
        ".xlsx": head.startswith(b"PK"),
        ".docx": head.startswith(b"PK"),
        ".xls": head.startswith(b"\xd0\xcf\x11\xe0"),
        ".doc": head.startswith(b"\xd0\xcf\x11\xe0"),
    }
    if suffix in signatures and not signatures[suffix]:
        raise ValidationError(f"文档 {upload.name} 的文件头与扩展名不一致")
    return "application/octet-stream"


def _text(value):
    return "" if value is None else str(value).strip()


def normalize_school_year(value):
    text = _text(value)
    match = re.fullmatch(r"(20\d{2})(?:[-/]20\d{2})?", text)
    if not match or match.group(1) not in GRADE_BY_YEAR:
        raise ValidationError("学年只能填写 2024、2025 或 2026，例如 2026")
    return match.group(1)


def grade_for(department_name, school_year):
    department = "middle" if department_name == "初中部" else "high" if department_name == "高中部" else department_name
    try:
        return GRADE_BY_YEAR[normalize_school_year(school_year)][department]
    except KeyError as exc:
        raise ValidationError("学部只能是初中部或高中部") from exc


def parse_roster(upload):
    if upload.size > settings.MAX_UPLOAD_SIZE_MB * 4 * 1024 * 1024:
        raise ValidationError("Excel 文件过大")
    try:
        upload.seek(0)
        book = load_workbook(upload, read_only=True, data_only=True)
        sheet = book.active
        rows = list(sheet.iter_rows(values_only=True))
    except Exception as exc:
        raise ValidationError("Excel 无法读取，请使用系统模板保存为 .xlsx") from exc
    if not rows:
        raise ValidationError("Excel 没有数据")
    header_index = next((index for index, row in enumerate(rows[:10]) if {"姓名", "学部", "学年", "班级", "就读类型"}.issubset({_text(v) for v in row})), None)
    if header_index is None:
        header_index = 0
    header = [_text(v) for v in rows[header_index]]
    missing = [h for h in ["姓名", "学部", "学年", "班级", "就读类型"] if h not in header]
    if missing:
        raise ValidationError("Excel 缺少必填列：" + "、".join(missing))
    positions = {h: i for i, h in enumerate(header) if h}
    records, errors = [], []
    seen = set()
    for row_no, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not any(v not in (None, "") for v in row):
            continue
        record = {h: _text(row[i]) if i < len(row) else "" for h, i in positions.items()}
        row_errors = []
        for field in ["姓名", "学部", "学年", "班级", "就读类型"]:
            if not record.get(field):
                row_errors.append(f"缺少{field}")
        if record.get("性别") and record["性别"] not in {"男", "女"}:
            row_errors.append("性别只能是男或女")
        if record.get("学部") not in {"初中部", "高中部"}:
            row_errors.append("学部只能是初中部或高中部")
        try:
            record["学年"] = normalize_school_year(record.get("学年"))
            record["年级"] = grade_for(record["学部"], record["学年"])
        except ValidationError as exc:
            row_errors.append(str(exc))
        if record.get("就读类型") not in {"走读", "半走读", "住宿"}:
            row_errors.append("就读类型不正确")
        identity = (record.get("学年"), record.get("学部"), record.get("班级"), record.get("姓名"), record.get("性别"), record.get("就读类型"))
        if identity in seen:
            row_errors.append("Excel 内重复学生记录")
        seen.add(identity)
        if row_errors:
            errors.append({"row": row_no, "errors": row_errors, "record": record})
        records.append(record)
    if errors:
        raise ValidationError("Excel 存在逐行错误", errors)
    if not records:
        raise ValidationError("Excel 没有有效学生记录")
    return records


def _department_code(value):
    return "初中部" if value == "middle" else "高中部"


def _class_from_record(record):
    department = "middle" if record["学部"] == "初中部" else "high"

    def operation():
        with transaction.atomic():
            school_year, _ = SchoolYear.objects.get_or_create(name=record["学年"], defaults={"is_active": True})
            class_group, _ = ClassGroup.objects.get_or_create(
                department=department,
                school_year=school_year,
                grade=grade_for(record["学部"], record["学年"]),
                name=record["班级"],
                defaults={"is_active": True},
            )
            return school_year, class_group

    return _with_db_retry(operation)


def _match_photos(records, photo_uploads):
    by_name = defaultdict(list)
    for upload in photo_uploads:
        by_name[upload.name].append(upload)
        by_name[PurePosixPath(upload.name).stem].append(upload)
    by_student_name = defaultdict(list)
    for record in records:
        by_student_name[record["姓名"]].append(record)
    matches = {}
    used = set()
    for index, record in enumerate(records):
        if len(by_student_name[record["姓名"]]) > 1:
            matches[index] = "ambiguous"
            continue
        candidates = by_name.get(record["姓名"], []) or by_name.get(PurePosixPath(record["姓名"]).stem, [])
        candidates = [u for u in candidates if id(u) not in used]
        matches[index] = candidates[0] if len(candidates) == 1 else ("ambiguous" if len(candidates) > 1 else None)
        if candidates and len(candidates) == 1:
            used.add(id(candidates[0]))
    return matches


def _prepare_photos(photo_uploads):
    prepared = []
    total_size = 0
    for upload in photo_uploads:
        content_type, sha256 = image_bytes_info(upload)
        prepared.append(PreparedUpload(upload.name, _safe_upload_name(upload), content_type, upload.size, sha256, source=upload))
        total_size += upload.size
    if total_size > settings.MAX_BATCH_UPLOAD_SIZE_MB * 1024 * 1024:
        raise ValidationError(f"本批照片总大小不能超过 {settings.MAX_BATCH_UPLOAD_SIZE_MB} MB")
    return prepared


def _prepare_documents(document_uploads):
    prepared = []
    total_size = 0
    for upload in document_uploads:
        content_type = document_info(upload)
        prepared.append(PreparedUpload(upload.name, _safe_upload_name(upload), content_type, upload.size, "", source=upload))
        total_size += upload.size
    if total_size > settings.MAX_BATCH_UPLOAD_SIZE_MB * 1024 * 1024:
        raise ValidationError(f"本批文档总大小不能超过 {settings.MAX_BATCH_UPLOAD_SIZE_MB} MB")
    return prepared


def _store_prepared_upload(candidate, target):
    if candidate.stored_name:
        source = default_storage.open(candidate.stored_name, "rb")
        close_source = True
    else:
        source = candidate.source
        source.seek(0)
        close_source = False
    try:
        stored_name = default_storage.save(target, source)
    finally:
        if close_source:
            source.close()
    return stored_name


def _delete_stored_files(names):
    for name in names:
        if name:
            try:
                default_storage.delete(name)
            except OSError:
                pass


def _check_storage_quota(additional_bytes):
    root = Path(settings.MEDIA_ROOT)
    used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) if root.exists() else 0
    quota = settings.MAX_STORAGE_MB * 1024 * 1024
    if used + additional_bytes > quota:
        raise ValidationError(f"文件存储配额不足，当前配额为 {settings.MAX_STORAGE_MB} MB")


def _create_submission_records(*, records, roster_name, roster_candidate, photo_inputs, document_inputs, note, ip_address, access_token=None):
    scopes = {(r.get("学部"), r.get("学年"), r.get("班级")) for r in records}
    if len(scopes) != 1:
        raise ValidationError("一次提交只能包含同一学部、学年和班级的学生")
    collection_scope = _with_db_retry(lambda: AppSetting.get_value("collection_scope", "all"))
    if collection_scope == "configured":
        missing_scopes = []
        for department_name, year_name, class_name in scopes:
            department = "middle" if department_name == "初中部" else "high"
            grade = grade_for(department_name, year_name)
            configured = _with_db_retry(lambda: ClassGroup.objects.filter(department=department, school_year__name=year_name, grade=grade, name=class_name, is_active=True).exists())
            if not configured:
                missing_scopes.append(f"{department_name}/{year_name}/{grade}/{class_name}")
        if missing_scopes:
            raise ValidationError("当前仅允许已配置班级提交：" + "、".join(missing_scopes))

    school_year, class_group = _class_from_record(records[0])
    token = access_token or Submission.make_token()
    matches = _match_photos(records, photo_inputs)
    approved_names = _with_db_retry(lambda: set(
        Student.objects.filter(
            school_year=school_year,
            department=class_group.department,
            grade=class_group.grade,
            class_name=records[0]["班级"],
            submission__status="approved",
        ).values_list("name", flat=True)
    ))

    def create_rows():
        with transaction.atomic():
            submission = Submission.objects.create(
                access_token_hash=hashlib.sha256(token.encode()).hexdigest(),
                department=class_group.department,
                school_year=school_year,
                grade=class_group.grade,
                class_group=class_group,
                source_filename=safe_filename(roster_name),
                note=note[:500],
            )
            students = [
                Student(
                    submission=submission,
                    student_no="",
                    name=record["姓名"],
                    gender=record.get("性别", ""),
                    department=class_group.department,
                    school_year=school_year,
                    grade=class_group.grade,
                    class_name=record["班级"],
                    study_type=record["就读类型"],
                    photo_filename_hint="",
                    remark=record.get("备注", ""),
                    match_status="matched" if isinstance(matches[index], PreparedUpload) else ("ambiguous" if matches[index] == "ambiguous" else "missing"),
                    is_duplicate=record["姓名"] in approved_names,
                )
                for index, record in enumerate(records)
            ]
            students = Student.objects.bulk_create(students)
            return submission, students

    submission = None
    stored_names = []
    try:
        submission, students = _with_db_retry(create_rows)
        roster_stored_name = _store_prepared_upload(roster_candidate, f"submissions/{submission.id}/roster/{safe_filename(roster_name)}")
        stored_names.append(roster_stored_name)

        for candidate in photo_inputs:
            stored = _store_prepared_upload(candidate, f"submissions/{submission.id}/photos/{candidate.original_name}")
            candidate.stored_name = stored
            stored_names.append(stored)
        for candidate in document_inputs:
            stored = _store_prepared_upload(candidate, f"submissions/{submission.id}/documents/{candidate.original_name}")
            candidate.stored_name = stored
            stored_names.append(stored)

        def create_files_and_audit():
            with transaction.atomic():
                Submission.objects.filter(pk=submission.pk).update(roster_file=roster_stored_name)
                photos = []
                matched_ids = set()
                student_by_index = {index: student for index, student in enumerate(students)}
                for index, matched in matches.items():
                    if isinstance(matched, PreparedUpload):
                        photos.append(StudentPhoto(student=student_by_index[index], submission=submission, original_name=matched.original_name, file=matched.stored_name, content_type=matched.content_type, size=matched.size, sha256=matched.sha256))
                        matched_ids.add(id(matched))
                for candidate in photo_inputs:
                    if id(candidate) not in matched_ids:
                        photos.append(StudentPhoto(student=None, submission=submission, original_name=candidate.original_name, file=candidate.stored_name, content_type=candidate.content_type, size=candidate.size, sha256=candidate.sha256))
                if photos:
                    StudentPhoto.objects.bulk_create(photos)
                documents = [SubmissionDocument(submission=submission, original_name=candidate.original_name, file=candidate.stored_name, content_type=candidate.content_type, size=candidate.size) for candidate in document_inputs]
                if documents:
                    SubmissionDocument.objects.bulk_create(documents)
                AuditLog.objects.create(action="public_submit", target_type="Submission", target_id=str(submission.id), detail={"student_count": len(records)}, ip_address=ip_address)

        _with_db_retry(create_files_and_audit)
        _with_db_retry(lambda: submission.refresh_from_db())
        return submission, token
    except Exception:
        _delete_stored_files(stored_names)
        if submission is not None:
            Submission.objects.filter(pk=submission.pk).delete()
        raise


def create_submission(*, roster_upload, photo_uploads, document_uploads, note="", ip_address=None):
    records = parse_roster(roster_upload)
    if len(photo_uploads) > settings.MAX_PHOTOS_PER_SUBMISSION:
        raise ValidationError(f"照片数量不能超过 {settings.MAX_PHOTOS_PER_SUBMISSION} 张")
    if len(document_uploads) > settings.MAX_DOCUMENTS_PER_SUBMISSION:
        raise ValidationError(f"文档数量不能超过 {settings.MAX_DOCUMENTS_PER_SUBMISSION} 个")
    if len(photo_uploads) > settings.MAX_PHOTOS_PER_BATCH or len(document_uploads) > settings.MAX_DOCUMENTS_PER_BATCH:
        raise ValidationError("照片或文档数量超过单批上限，请使用分批上传")
    prepared_photos = _prepare_photos(photo_uploads)
    prepared_documents = _prepare_documents(document_uploads)
    _check_storage_quota(roster_upload.size + sum(item.size for item in prepared_photos + prepared_documents))
    return _create_submission_records(
        records=records,
        roster_name=roster_upload.name,
        roster_candidate=PreparedUpload(roster_upload.name, _safe_upload_name(roster_upload), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", roster_upload.size, "", source=roster_upload),
        photo_inputs=prepared_photos,
        document_inputs=prepared_documents,
        note=note,
        ip_address=ip_address,
    )


def _manifest_digest(prepared):
    digest = hashlib.sha256()
    for item in prepared:
        digest.update(item.name.encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(str(item.size).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _check_upload_token(session, token):
    if not token or hashlib.sha256(token.encode()).hexdigest() != session.access_token_hash:
        raise ValidationError("上传凭证无效")


def start_upload_session(*, roster_upload, document_uploads, expected_photo_count=0, photo_manifest=None, roster_fingerprint="", note="", ip_address=None):
    if expected_photo_count < 0 or expected_photo_count > settings.MAX_PHOTOS_PER_SUBMISSION:
        raise ValidationError(f"照片数量不能超过 {settings.MAX_PHOTOS_PER_SUBMISSION} 张")
    if len(document_uploads) > settings.MAX_DOCUMENTS_PER_SUBMISSION:
        raise ValidationError(f"文档数量不能超过 {settings.MAX_DOCUMENTS_PER_SUBMISSION} 个")
    records = parse_roster(roster_upload)
    normalized_manifest = _normalize_photo_manifest(photo_manifest, expected_photo_count)
    documents = _prepare_documents(document_uploads)
    _check_storage_quota(roster_upload.size + sum(item.size for item in documents))
    roster_sha256 = upload_sha256(roster_upload)
    token = UploadSession.make_token()
    session = UploadSession.objects.create(
        access_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        roster_original_name=_safe_upload_name(roster_upload),
        roster_sha256=roster_sha256,
        roster_fingerprint=str(roster_fingerprint or "")[:255],
        note=note[:500],
        expected_photo_count=expected_photo_count,
        expected_document_count=len(documents),
        photo_manifest=normalized_manifest,
        photo_manifest_digest=_manifest_json_digest(normalized_manifest),
    )
    stored_names = []
    try:
        roster_name = default_storage.save(f"upload_sessions/{session.id}/roster/{_safe_upload_name(roster_upload)}", roster_upload)
        stored_names.append(roster_name)
        UploadSession.objects.filter(pk=session.pk).update(roster_file=roster_name)
        if documents:
            stage_upload_batch(
                session.id,
                "document",
                0,
                documents,
                manifest_digest=session.photo_manifest_digest,
                roster_fingerprint=session.roster_fingerprint,
                already_prepared=True,
            )
        return session, token, len(records)
    except Exception:
        _delete_stored_files(stored_names)
        session.delete()
        raise


def stage_upload_batch(session_id, kind, sequence, uploads, *, token=None, manifest_digest=None, roster_fingerprint=None, already_prepared=False):
    if kind not in {"photo", "document"}:
        raise ValidationError("上传批次类型不正确")
    if sequence < 0:
        raise ValidationError("上传批次编号不正确")
    if not uploads:
        raise ValidationError("本批没有文件")
    if kind == "photo" and len(uploads) > settings.MAX_PHOTOS_PER_BATCH:
        raise ValidationError(f"每批最多上传 {settings.MAX_PHOTOS_PER_BATCH} 张照片")
    if kind == "document" and len(uploads) > settings.MAX_DOCUMENTS_PER_BATCH:
        raise ValidationError(f"每批最多上传 {settings.MAX_DOCUMENTS_PER_BATCH} 个文档")
    prepared = uploads if already_prepared else (_prepare_photos(uploads) if kind == "photo" else _prepare_documents(uploads))
    if sum(item.size for item in prepared) > settings.MAX_BATCH_UPLOAD_SIZE_MB * 1024 * 1024:
        raise ValidationError(f"本批文件总大小不能超过 {settings.MAX_BATCH_UPLOAD_SIZE_MB} MB")
    digest = _manifest_digest(prepared)
    session = UploadSession.objects.get(pk=session_id)
    if token is not None:
        _check_upload_token(session, token)
    if session.photo_manifest_digest and manifest_digest != session.photo_manifest_digest:
        raise ValidationError("照片清单已变化，请重新选择 Excel 和照片后提交")
    if session.roster_fingerprint and roster_fingerprint != session.roster_fingerprint:
        raise ValidationError("Excel 文件已变化，请重新选择 Excel 和照片后提交")
    if session.status != "pending":
        raise ValidationError("上传会话已结束")

    def existing_batch():
        with transaction.atomic():
            locked_session = UploadSession.objects.select_for_update().get(pk=session_id)
            if locked_session.status != "pending":
                raise ValidationError("上传会话已结束")
            if locked_session.photo_manifest_digest and manifest_digest != locked_session.photo_manifest_digest:
                raise ValidationError("照片清单已变化，请重新选择 Excel 和照片后提交")
            if locked_session.roster_fingerprint and roster_fingerprint != locked_session.roster_fingerprint:
                raise ValidationError("Excel 文件已变化，请重新选择 Excel 和照片后提交")
            existing = UploadBatch.objects.filter(session=locked_session, kind=kind, sequence=sequence).first()
            if existing:
                if existing.digest != digest or existing.count != len(prepared):
                    raise ValidationError("该上传批次已存在不同内容")
                return existing
            if kind == "photo" and locked_session.photo_manifest:
                previous = list(UploadBatch.objects.filter(session=locked_session, kind="photo").order_by("sequence"))
                if [item.sequence for item in previous] != list(range(sequence)):
                    raise ValidationError("照片批次必须按顺序上传，请从上一个成功批次继续")
                offset = sum(item.count for item in previous)
                expected = locked_session.photo_manifest[offset:offset + len(prepared)]
                if len(expected) != len(prepared):
                    raise ValidationError("照片数量超过提交时的照片清单")
                for expected_item, actual_item in zip(expected, prepared):
                    if expected_item["name"] != actual_item.original_name or expected_item["size"] != actual_item.size:
                        raise ValidationError("照片名称、大小或顺序已变化，请重新选择 Excel 和照片后提交")
                    if expected_item.get("sha256") and expected_item["sha256"] != actual_item.sha256:
                        raise ValidationError("照片内容已变化，请重新选择 Excel 和照片后提交")
            return None

    already_saved = _with_db_retry(existing_batch)
    if already_saved is not None:
        return already_saved, len(prepared)
    _check_storage_quota(sum(item.size for item in prepared))

    stored_names = []
    try:
        for item in prepared:
            stored_names.append(_store_prepared_upload(item, f"upload_sessions/{session.id}/{kind}/{sequence}/{item.original_name}"))

        def save_batch():
            with transaction.atomic():
                locked_session = UploadSession.objects.select_for_update().get(pk=session_id)
                if locked_session.status != "pending":
                    raise ValidationError("上传会话已结束")
                existing = UploadBatch.objects.filter(session=locked_session, kind=kind, sequence=sequence).first()
                if existing:
                    if existing.digest != digest or existing.count != len(prepared):
                        raise ValidationError("该上传批次已存在不同内容")
                    return existing
                batch = UploadBatch.objects.create(session=locked_session, kind=kind, sequence=sequence, digest=digest, count=len(prepared))
                StagedUpload.objects.bulk_create([
                    StagedUpload(batch=batch, ordinal=index, original_name=item.original_name, file=stored_names[index], content_type=item.content_type, size=item.size, sha256=item.sha256)
                    for index, item in enumerate(prepared)
                ])
                return batch

        batch = _with_db_retry(save_batch)
        if batch.count == len(prepared) and batch.digest == digest:
            # A retried batch is already persisted; its newly written files are not needed.
            persisted = list(batch.uploads.order_by("ordinal"))
            if persisted and all(item.file.name != stored_names[index] for index, item in enumerate(persisted)):
                _delete_stored_files(stored_names)
        return batch, len(prepared)
    except Exception:
        _delete_stored_files(stored_names)
        raise


def finalize_upload_session(*, session_id, token, manifest_digest=None, roster_fingerprint=None, ip_address=None):
    session = UploadSession.objects.get(pk=session_id)
    _check_upload_token(session, token)

    def claim_session():
        with transaction.atomic():
            locked = UploadSession.objects.select_for_update().select_related("completed_submission").get(pk=session_id)
            _check_upload_token(locked, token)
            if locked.photo_manifest_digest and manifest_digest != locked.photo_manifest_digest:
                raise ValidationError("照片清单已变化，请重新选择 Excel 和照片后提交")
            if locked.roster_fingerprint and roster_fingerprint != locked.roster_fingerprint:
                raise ValidationError("Excel 文件已变化，请重新选择 Excel 和照片后提交")
            if locked.status == "completed" and locked.completed_submission_id:
                return locked.completed_submission, False
            if locked.status != "pending":
                raise ValidationError("上传会话正在完成或已结束")
            locked.status = "finalizing"
            locked.save(update_fields=["status", "updated_at"])
            return locked, True

    claimed, should_finalize = _with_db_retry(claim_session)
    if not should_finalize:
        return claimed, _session_result_token(session_id)
    session = claimed
    try:
        roster_path = session.roster_file.name
        if not roster_path or not default_storage.exists(roster_path):
            raise ValidationError("学生名单文件不存在，请重新上传")
        with default_storage.open(roster_path, "rb") as roster_source:
            roster_data = roster_source.read()
        from django.core.files.uploadedfile import SimpleUploadedFile
        records = parse_roster(SimpleUploadedFile(session.roster_original_name, roster_data))
        photo_rows = list(StagedUpload.objects.filter(batch__session=session, batch__kind="photo").select_related("batch").order_by("batch__sequence", "ordinal"))
        document_rows = list(StagedUpload.objects.filter(batch__session=session, batch__kind="document").select_related("batch").order_by("batch__sequence", "ordinal"))
        if len(photo_rows) != session.expected_photo_count:
            raise ValidationError(f"照片尚未全部上传，已收到 {len(photo_rows)} 张，应为 {session.expected_photo_count} 张")
        if len(document_rows) != session.expected_document_count:
            raise ValidationError("相关文档尚未全部上传")
        photo_inputs = [PreparedUpload(row.original_name, row.original_name, row.content_type, row.size, row.sha256, stored_name=row.file.name) for row in photo_rows]
        document_inputs = [PreparedUpload(row.original_name, row.original_name, row.content_type, row.size, row.sha256, stored_name=row.file.name) for row in document_rows]
        roster_candidate = PreparedUpload(session.roster_original_name, session.roster_original_name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 0, "", stored_name=roster_path)
        result_token = _session_result_token(session.id)
        submission, result_token = _create_submission_records(
            records=records,
            roster_name=session.roster_original_name,
            roster_candidate=roster_candidate,
            photo_inputs=photo_inputs,
            document_inputs=document_inputs,
            note=session.note,
            ip_address=ip_address,
            access_token=result_token,
        )
        def complete():
            with transaction.atomic():
                locked = UploadSession.objects.select_for_update().get(pk=session.id)
                locked.status = "completed"
                locked.completed_submission = submission
                locked.save(update_fields=["status", "completed_submission", "updated_at"])
        _with_db_retry(complete)
        _delete_stored_files([roster_path] + [row.file.name for row in photo_rows + document_rows])
        return submission, result_token
    except Exception:
        UploadSession.objects.filter(pk=session.id, status="finalizing").update(status="pending")
        raise


def validate_naming_rule(rule):
    if rule.mode == "name":
        rule.template = "{姓名}"
    elif rule.mode == "gender_no":
        rule.template = "{姓名}" + rule.separator + "{性别编号}"
    # Normalize templates saved by older versions before validating the new field set.
    rule.template = rule.template.replace("{学生编号}", "{姓名}").replace("{年级}", "")
    tokens = set(re.findall(r"\{[^{}]+\}", rule.template))
    if not tokens.issubset(ALLOWED_TOKENS):
        raise ValidationError("命名模板包含不支持的字段")
    if any(part in rule.template for part in ["/", "\\", ".."]):
        raise ValidationError("命名模板不能包含路径片段")
    return rule


def review_submission_version(*, submission_id, action, actor, ip_address=None):
    if action not in {"approved", "rejected"}:
        raise ValidationError("审核操作不正确")

    def operation():
        with transaction.atomic():
            submission = Submission.objects.select_for_update().select_related("class_group").get(pk=submission_id)
            if action == "approved":
                Submission.objects.filter(class_group=submission.class_group, status="approved").exclude(pk=submission.pk).update(status="rejected", reviewed_at=timezone.now(), reviewed_by=actor)
            submission.status = action
            submission.reviewed_at = timezone.now()
            submission.reviewed_by = actor
            submission.save(update_fields=["status", "reviewed_at", "reviewed_by"])
            AuditLog.objects.create(actor=actor, action=f"submission_{action}", target_type="Submission", target_id=str(submission.id), ip_address=ip_address)
            return submission

    return _with_db_retry(operation)


def render_filename(student, rule, used):
    gender_code = ""
    if student.gender == "男":
        gender_code = rule.male_code
    elif student.gender == "女":
        gender_code = rule.female_code
    values = {
        "{姓名}": student.name,
        "{性别编号}": gender_code,
        "{学部}": _department_code(student.department),
        "{班级}": student.class_name,
        "{就读类型}": student.study_type,
    }
    base = safe_filename(rule.template)
    for token, value in values.items():
        base = base.replace(token, safe_filename(value, fallback=""))
    base = base or safe_filename(student.name)
    source_suffix = PurePosixPath(student.photo.original_name).suffix.lower() or ".jpg"
    candidate = f"{base}{source_suffix}"
    counter = 2
    while candidate in used:
        candidate = f"{base}_{counter}{source_suffix}"
        counter += 1
    used.add(candidate)
    return candidate


def _write_workbook(students, label, anomalies, filename_preview=True):
    book = Workbook()
    summary = book.active
    summary.title = "统计汇总"
    total = len(students)
    summary_rows = [
        ("范围", label), ("总人数", total), ("男生人数", sum(s.gender == "男" for s in students)),
        ("女生人数", sum(s.gender == "女" for s in students)), ("走读人数", sum(s.study_type == "走读" for s in students)),
        ("半走读人数", sum(s.study_type == "半走读" for s in students)), ("住宿人数", sum(s.study_type == "住宿" for s in students)),
        ("成功导出照片数", sum(bool(getattr(s, "_export_name", "")) for s in students)),
        ("缺失照片数", sum(not bool(getattr(s, "_export_name", "")) for s in students)),
    ]
    for row in summary_rows: summary.append([safe_excel_value(value) for value in row])
    detail = book.create_sheet("学生明细")
    detail.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "实际导出照片文件名", "匹配状态"])
    for student in students:
        detail.append([safe_excel_value(value) for value in [student.name, student.gender, _department_code(student.department), student.school_year.name, student.class_name, student.study_type, getattr(student, "_export_name", ""), getattr(student, "_export_status", student.get_match_status_display())]])
    anomaly_sheet = book.create_sheet("异常记录")
    anomaly_sheet.append(["类型", "学生", "详情"])
    for item in anomalies:
        anomaly_sheet.append([safe_excel_value(value) for value in item])
    for sheet in book.worksheets:
        for column in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(column)].width = min(max(12, max(len(str(sheet.cell(r, column).value or "")) for r in range(1, sheet.max_row + 1)) + 2), 42)
        sheet.freeze_panes = "A2"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _student_photo(student):
    try:
        return student.photo
    except StudentPhoto.DoesNotExist:
        return None


def export_students_to_tempfile(filters, actor=None, ip_address=None):
    qs = Student.objects.select_related("submission", "school_year").filter(submission__status="approved")
    for key in ["school_year__name", "department", "grade", "class_name", "gender", "study_type"]:
        if filters.get(key): qs = qs.filter(**{key: filters[key]})
    students = list(qs.order_by("department", "grade", "class_name", "name"))
    if filters.get("has_photo") == "yes": students = [s for s in students if s.match_status == "matched" and hasattr(s, "photo")]
    if filters.get("has_photo") == "no": students = [s for s in students if not (s.match_status == "matched" and hasattr(s, "photo"))]
    rule = validate_naming_rule(NamingRule.current())
    groups = defaultdict(list)
    for student in students: groups[(student.department, student.grade, student.class_name)].append(student)
    Path(settings.EXPORT_TEMP_DIR).mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="student-export-", suffix=".zip", dir=settings.EXPORT_TEMP_DIR)
    os.close(fd)
    anomalies_total = []
    split_study_types = not filters.get("study_type")
    study_type_order = ["走读", "半走读", "住宿"]
    try:
        with zipfile.ZipFile(temp_name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for group_key, group_students in groups.items():
                dept, grade, class_name = group_key
                class_dir = "/".join(safe_filename(v) for v in [_department_code(dept), grade, class_name])
                class_submissions = list(Submission.objects.filter(students__in=group_students).distinct())
                class_file_anomalies = []
                roster_names = set()
                for submission in class_submissions:
                    if submission.roster_file:
                        roster_name = safe_filename(submission.source_filename or PurePosixPath(submission.roster_file.name).name)
                        if roster_name in roster_names:
                            stem = PurePosixPath(roster_name).stem
                            suffix = PurePosixPath(roster_name).suffix
                            roster_name = f"{str(submission.id)[:8]}_{stem}{suffix}"
                        roster_names.add(roster_name)
                        try:
                            with submission.roster_file.open("rb") as source, archive.open(f"{class_dir}/学生名单/{roster_name}", "w") as target:
                                shutil.copyfileobj(source, target, length=1024 * 1024)
                        except (FileNotFoundError, OSError, ValueError) as exc:
                            class_file_anomalies.append(("文件缺失", "学生名单", f"{roster_name}：{exc}"))

                document_names = set()
                for document in SubmissionDocument.objects.filter(submission__in=class_submissions).distinct():
                    document_name = safe_filename(document.original_name)
                    if document_name in document_names:
                        stem = PurePosixPath(document_name).stem
                        suffix = PurePosixPath(document_name).suffix
                        document_name = f"{str(document.id)}_{stem}{suffix}"
                    document_names.add(document_name)
                    try:
                        with document.file.open("rb") as source, archive.open(f"{class_dir}/相关文档/{document_name}", "w") as target:
                            shutil.copyfileobj(source, target, length=1024 * 1024)
                    except (FileNotFoundError, OSError, ValueError) as exc:
                        class_file_anomalies.append(("文件缺失", "相关文档", f"{document_name}：{exc}"))

                category_groups = defaultdict(list)
                if split_study_types:
                    for student in group_students: category_groups[student.study_type].append(student)
                else:
                    category_groups[""] = group_students
                for study_type in study_type_order if split_study_types else [""]:
                    category_students = category_groups.get(study_type, [])
                    if not category_students:
                        continue
                    used, photo_anomalies = set(), []
                    category_dir = f"{class_dir}/{safe_filename(study_type)}" if split_study_types else class_dir
                    for student in category_students:
                        student._export_name = ""
                        student._export_status = student.get_match_status_display()
                        photo = _student_photo(student)
                        if student.match_status == "matched" and photo is not None:
                            candidate_name = render_filename(student, rule, used)
                            try:
                                with photo.file.open("rb") as source, archive.open(f"{category_dir}/学生照片/{candidate_name}", "w") as target:
                                    shutil.copyfileobj(source, target, length=1024 * 1024)
                            except (FileNotFoundError, OSError, ValueError) as exc:
                                student._export_status = "文件缺失"
                                photo_anomalies.append(("文件缺失", student.name, f"照片无法读取：{photo.file.name}；{exc}"))
                            else:
                                student._export_name = candidate_name
                                student._export_status = "已导出"
                        else:
                            photo_anomalies.append((student.get_match_status_display(), student.name, "没有可导出的已匹配照片"))
                    archive.writestr(f"{category_dir}/学生统计表.xlsx", _write_workbook(category_students, f"{category_dir}", photo_anomalies + class_file_anomalies))
                    anomalies_total.extend(photo_anomalies)
                anomalies_total.extend(class_file_anomalies)
            if len(groups) > 1:
                archive.writestr("总统计表.xlsx", _write_workbook(students, "跨班级导出", anomalies_total))
        _with_db_retry(lambda: AuditLog.objects.create(actor=actor, action="export_students", target_type="Student", detail={"filters": filters, "count": len(students)}, ip_address=ip_address))
        return Path(temp_name), len(students), [getattr(s, "_export_name", "") for s in students[:10]]
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def export_students(filters, actor=None, ip_address=None):
    temp_path, count, preview = export_students_to_tempfile(filters, actor=actor, ip_address=ip_address)
    try:
        return temp_path.read_bytes(), count, preview
    finally:
        temp_path.unlink(missing_ok=True)


@transaction.atomic
def clear_rejected_submissions(actor=None, ip_address=None):
    submissions = list(Submission.objects.filter(status="rejected").prefetch_related("photos", "documents"))
    removed_students = 0
    removed_files = 0
    for submission in submissions:
        removed_students += submission.students.count()
        for obj in list(submission.photos.all()) + list(submission.documents.all()):
            obj.file.delete(save=False)
            removed_files += 1
        if submission.roster_file:
            submission.roster_file.delete(save=False)
            removed_files += 1
        submission.delete()
    AuditLog.objects.create(actor=actor, action="clear_rejected_submissions", detail={"submissions": len(submissions), "students": removed_students, "files": removed_files}, ip_address=ip_address)
    return len(submissions), removed_students, removed_files


@transaction.atomic
def clear_expired_upload_sessions(days, actor=None, ip_address=None):
    cutoff = timezone.now() - timedelta(days=days)
    sessions = list(UploadSession.objects.filter(updated_at__lt=cutoff).prefetch_related("batches__uploads"))
    removed_files = 0
    for session in sessions:
        if session.roster_file:
            session.roster_file.delete(save=False)
            removed_files += 1
        for batch in session.batches.all():
            for staged in batch.uploads.all():
                staged.file.delete(save=False)
                removed_files += 1
        session.delete()
    if sessions:
        AuditLog.objects.create(actor=actor, action="clear_expired_upload_sessions", detail={"sessions": len(sessions), "files": removed_files}, ip_address=ip_address)
    return len(sessions), removed_files


@transaction.atomic
def clear_all_collection_data():
    """Reset collection data while preserving administrator accounts."""
    photos = list(StudentPhoto.objects.all())
    documents = list(SubmissionDocument.objects.all())
    submissions = list(Submission.objects.all())
    staged_uploads = list(StagedUpload.objects.all())
    upload_sessions = list(UploadSession.objects.all())
    roster_count = sum(bool(submission.roster_file) for submission in submissions)
    for obj in photos + documents:
        obj.file.delete(save=False)
    for submission in submissions:
        if submission.roster_file:
            submission.roster_file.delete(save=False)
    for staged in staged_uploads:
        staged.file.delete(save=False)
    for session in upload_sessions:
        if session.roster_file:
            session.roster_file.delete(save=False)

    counts = {
        "submissions": Submission.objects.count(),
        "students": Student.objects.count(),
        "photos": len(photos),
        "documents": len(documents),
        "rosters": roster_count,
        "classes": ClassGroup.objects.count(),
        "school_years": SchoolYear.objects.count(),
        "naming_rules": NamingRule.objects.count(),
        "settings": AppSetting.objects.count(),
        "audit_logs": AuditLog.objects.count(),
        "staged_uploads": len(staged_uploads),
        "upload_sessions": len(upload_sessions),
    }
    Submission.objects.all().delete()
    Student.objects.all().delete()
    ClassGroup.objects.all().delete()
    SchoolYear.objects.all().delete()
    NamingRule.objects.all().delete()
    AppSetting.objects.all().delete()
    AuditLog.objects.all().delete()

    submissions_root = Path(settings.MEDIA_ROOT) / "submissions"
    shutil.rmtree(submissions_root, ignore_errors=True)
    submissions_root.mkdir(parents=True, exist_ok=True)
    upload_sessions_root = Path(settings.MEDIA_ROOT) / "upload_sessions"
    shutil.rmtree(upload_sessions_root, ignore_errors=True)
    return counts


def template_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "学生名单"
    last_column = get_column_letter(len(HEADERS))
    sheet.merge_cells(f"A1:{last_column}1")
    sheet["A1"] = "填写说明：一行一位学生；姓名、学部、学年、班级、就读类型为必填；年级由学部和学年自动判断。"
    sheet.merge_cells(f"A2:{last_column}2")
    sheet["A2"] = "学年填写当前在读学年的起始年份，不是入学年份：2026/2025/2024 分别对应高中高一/高二/高三、初中初一/初二/初三；照片默认按姓名匹配。"
    for cell_ref in ("A1", "A2"):
        sheet[cell_ref].font = Font(bold=True, color="FFFFFF" if cell_ref == "A1" else "17233B")
        sheet[cell_ref].fill = PatternFill("solid", fgColor="17233B" if cell_ref == "A1" else "E8F5F2")
        sheet[cell_ref].alignment = Alignment(vertical="center")
    for col, header in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=4, column=col, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="E76F3C")
    for col, value in enumerate(["示例同学", "男", "高中部", "2026", "1", "走读", "虚构示例，仅用于测试"], start=1):
        sheet.cell(row=5, column=col, value=value)
    sheet.freeze_panes = "A5"
    for col in range(1, len(HEADERS) + 1): sheet.column_dimensions[get_column_letter(col)].width = 18
    out = io.BytesIO(); book.save(out); return out.getvalue()

