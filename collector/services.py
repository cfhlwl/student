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
from django.db import IntegrityError, transaction
from django.db.utils import OperationalError
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from PIL import Image, UnidentifiedImageError

from .models import AppSetting, AuditLog, ClassGroup, NamingRule, PhotoSupplement, SchoolYear, StagedUpload, Student, StudentPhoto, Submission, SubmissionDocument, SupplementPhoto, UploadBatch, UploadSession

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
ALLOWED_TOKENS = {"{姓名}", "{性别编号}", "{学部}", "{班级}", "{就读类型}"}
FIELD_MAP = {
    "姓名": "name", "性别": "gender", "学部": "department", "学年": "school_year",
    "班级": "class_name", "就读类型": "study_type",
}
HEADERS = ["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "备注"]
LEGACY_GRADE_BY_YEAR = {
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


def _normalize_upload_manifest(value, expected_count, label):
    if value in (None, "", []):
        if expected_count:
            raise ValidationError(f"{label}清单不能为空，请重新选择文件后提交")
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{label}清单格式不正确，请重新选择文件") from exc
    if not isinstance(value, list) or len(value) != expected_count:
        raise ValidationError(f"{label}清单与文件数量不一致，请重新选择文件")
    manifest = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"第 {index} 个{label}清单项格式不正确")
        original_name = str(item.get("name") or "")
        name = safe_filename(original_name)
        if not original_name or name != original_name:
            raise ValidationError(f"第 {index} 个{label}文件名不安全")
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"第 {index} 个{label}大小不正确") from exc
        if size < 1:
            raise ValidationError(f"第 {index} 个{label}大小不正确")
        sha256 = str(item.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValidationError(f"第 {index} 个{label}哈希不正确或为空")
        manifest.append({"name": name, "size": size, "sha256": sha256})
    return manifest


def _normalize_photo_manifest(value, expected_count):
    return _normalize_upload_manifest(value, expected_count, "照片")


def _normalize_document_manifest(value, expected_count):
    return _normalize_upload_manifest(value, expected_count, "文档")


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
    if not match or match.group(1) not in LEGACY_GRADE_BY_YEAR:
        raise ValidationError("学年只能填写 2024、2025 或 2026，例如 2026")
    return match.group(1)


def normalize_grade(value, department_name=None):
    grade = _text(value)
    valid = {item[0] for item in ClassGroup.GRADES}
    if grade not in valid:
        raise ValidationError("年级只能填写初一、初二、初三、高一、高二或高三")
    if department_name == "初中部" and not grade.startswith("初"):
        raise ValidationError("初中部只能填写初一、初二或初三")
    if department_name == "高中部" and not grade.startswith("高"):
        raise ValidationError("高中部只能填写高一、高二或高三")
    return grade


def legacy_grade_for(department_name, school_year):
    department = "middle" if department_name == "初中部" else "high" if department_name == "高中部" else department_name
    try:
        return LEGACY_GRADE_BY_YEAR[normalize_school_year(school_year)][department]
    except KeyError as exc:
        raise ValidationError("学部只能是初中部或高中部") from exc


def grade_for(department_name, school_year):
    """Legacy compatibility helper for old rosters that omitted the grade column."""
    return legacy_grade_for(department_name, school_year)


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
            record["年级"] = normalize_grade(record["年级"], record["学部"]) if record.get("年级") else legacy_grade_for(record["学部"], record["学年"])
        except ValidationError as exc:
            row_errors.append(str(exc))
        if record.get("就读类型") not in set(STUDY_TYPE_VALUES):
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
                grade=record["年级"],
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
        prepared.append(PreparedUpload(upload.name, _safe_upload_name(upload), content_type, upload.size, upload_sha256(upload), source=upload))
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
    def file_size(path):
        try:
            return path.stat().st_size if path.is_file() else 0
        except (FileNotFoundError, OSError):
            # Finalization may remove a staged file between the directory scan
            # and stat(); the quota check must remain safe under concurrent uploads.
            return 0
    used = sum(file_size(path) for path in root.rglob("*")) if root.exists() else 0
    quota = settings.MAX_STORAGE_MB * 1024 * 1024
    if used + additional_bytes > quota:
        raise ValidationError(f"文件存储配额不足，当前配额为 {settings.MAX_STORAGE_MB} MB")


def _delete_submission_storage(submission_id):
    """Remove a private directory left by an interrupted finalization."""
    root = (Path(settings.MEDIA_ROOT) / "submissions" / str(submission_id)).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    if root == media_root or media_root not in root.parents:
        return
    if root.exists():
        shutil.rmtree(root)


def _file_matches_expected(file_field, expected_size=None, expected_sha256=""):
    """Return whether a private stored file is complete and, when available, hashed as expected."""
    if not file_field or not file_field.name or not default_storage.exists(file_field.name):
        return False
    try:
        digest = hashlib.sha256()
        total = 0
        with file_field.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
        if expected_size is not None and total != expected_size:
            return False
        return not expected_sha256 or hmac.compare_digest(digest.hexdigest(), expected_sha256)
    except (FileNotFoundError, OSError, ValueError):
        return False


def _submission_is_complete(submission, *, expected_student_count=None, expected_photo_count=None, expected_document_count=None):
    """Check every durable part before an upload session may be reported complete."""
    if not _file_matches_expected(submission.roster_file):
        return False
    if expected_student_count is not None and submission.students.count() != expected_student_count:
        return False
    photos = list(submission.photos.select_related("student"))
    if expected_photo_count is not None and len(photos) != expected_photo_count:
        return False
    documents = list(submission.documents.all())
    if expected_document_count is not None and len(documents) != expected_document_count:
        return False
    for photo in photos:
        if photo.student_id and photo.student.submission_id != submission.id:
            return False
        if not _file_matches_expected(photo.file, photo.size, photo.sha256):
            return False
    for document in documents:
        if not _file_matches_expected(document.file, document.size):
            return False
    return AuditLog.objects.filter(
        action="public_submit", target_type="Submission", target_id=str(submission.id)
    ).exists()


def _create_submission_records(*, records, roster_name, roster_candidate, photo_inputs, document_inputs, note, ip_address, access_token=None, expected_student_count=None, expected_photo_count=None, expected_document_count=None):
    scopes = {(r.get("学部"), r.get("学年"), r.get("年级"), r.get("班级")) for r in records}
    if len(scopes) != 1:
        raise ValidationError("一次提交只能包含同一学部、学年、年级和班级的学生")
    if access_token:
        access_hash = hashlib.sha256(access_token.encode()).hexdigest()
        existing_submission = _with_db_retry(lambda: Submission.objects.filter(access_token_hash=access_hash).first())
        if existing_submission is not None:
            if _submission_is_complete(
                existing_submission,
                expected_student_count=expected_student_count,
                expected_photo_count=expected_photo_count,
                expected_document_count=expected_document_count,
            ):
                return existing_submission, access_token
            partial_id = existing_submission.id
            existing_files = [existing_submission.roster_file.name]
            existing_files.extend(existing_submission.photos.values_list("file", flat=True))
            existing_files.extend(existing_submission.documents.values_list("file", flat=True))
            # A recovered finalization may still reference staged session files. They are
            # the inputs for the retry and must not be deleted with the partial Submission.
            protected_names = {roster_candidate.stored_name}
            protected_names.update(item.stored_name for item in photo_inputs if item.stored_name)
            protected_names.update(item.stored_name for item in document_inputs if item.stored_name)
            existing_submission.delete()
            _delete_stored_files(name for name in existing_files if name and name not in protected_names)
            _delete_submission_storage(partial_id)
    collection_scope = _with_db_retry(lambda: AppSetting.get_value("collection_scope", "all"))
    if collection_scope == "configured":
        missing_scopes = []
        for department_name, year_name, grade_name, class_name in scopes:
            department = "middle" if department_name == "初中部" else "high"
            configured = _with_db_retry(lambda: ClassGroup.objects.filter(department=department, school_year__name=year_name, grade=grade_name, name=class_name, is_active=True).exists())
            if not configured:
                missing_scopes.append(f"{department_name}/{year_name}/{grade_name}/{class_name}")
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


def start_upload_session(*, roster_upload, document_uploads, expected_photo_count=0, expected_document_count=None, photo_manifest=None, document_manifest=None, roster_fingerprint="", note="", ip_address=None, idempotency_key=""):
    if expected_photo_count < 0 or expected_photo_count > settings.MAX_PHOTOS_PER_SUBMISSION:
        raise ValidationError(f"照片数量不能超过 {settings.MAX_PHOTOS_PER_SUBMISSION} 张")
    if expected_document_count is None:
        expected_document_count = len(document_uploads)
    if expected_document_count < 0 or expected_document_count > settings.MAX_DOCUMENTS_PER_SUBMISSION:
        raise ValidationError(f"文档数量不能超过 {settings.MAX_DOCUMENTS_PER_SUBMISSION} 个")
    if len(document_uploads) > settings.MAX_DOCUMENTS_PER_SUBMISSION:
        raise ValidationError(f"文档数量不能超过 {settings.MAX_DOCUMENTS_PER_SUBMISSION} 个")
    records = parse_roster(roster_upload)
    normalized_manifest = _normalize_photo_manifest(photo_manifest, expected_photo_count)
    documents = _prepare_documents(document_uploads)
    if documents and len(documents) != expected_document_count:
        raise ValidationError("文档清单与文档数量不一致，请重新选择文件")
    if document_manifest in (None, "", []):
        # Older HTTP clients did not send a document manifest. Keep those
        # clients compatible; new clients send it so resumed sessions get the
        # same strict name/size/SHA256 validation as photos.
        normalized_document_manifest = [{"name": item.original_name, "size": item.size, "sha256": item.sha256} for item in documents] if documents else []
    else:
        normalized_document_manifest = _normalize_document_manifest(document_manifest, expected_document_count)
    if documents:
        for expected, actual in zip(normalized_document_manifest, documents):
            if expected != {"name": actual.original_name, "size": actual.size, "sha256": actual.sha256}:
                raise ValidationError("文档名称、大小或内容已变化，请重新选择文件")
    _check_storage_quota(roster_upload.size + sum(item.size for item in documents))
    roster_sha256 = upload_sha256(roster_upload)
    idempotency_key = str(idempotency_key or "")[:64]
    token = hmac.new(settings.SECRET_KEY.encode("utf-8"), f"student-collector-upload:{idempotency_key}".encode("utf-8"), hashlib.sha256).hexdigest() if idempotency_key else UploadSession.make_token()
    roster_original_name = _safe_upload_name(roster_upload)
    photo_manifest_digest = _manifest_json_digest(normalized_manifest)
    document_manifest_digest = _manifest_json_digest(normalized_document_manifest)
    def create_session():
        defaults = {
            "access_token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "roster_original_name": roster_original_name,
            "roster_sha256": roster_sha256,
            "roster_fingerprint": str(roster_fingerprint or "")[:255],
            "note": note[:500],
            "expected_student_count": len(records),
            "expected_photo_count": expected_photo_count,
            "expected_document_count": expected_document_count,
            "photo_manifest": normalized_manifest,
            "photo_manifest_digest": photo_manifest_digest,
            "document_manifest": normalized_document_manifest,
            "document_manifest_digest": document_manifest_digest,
        }
        if idempotency_key:
            session, created = UploadSession.objects.get_or_create(start_idempotency_key=idempotency_key, defaults=defaults)
            return session, created
        return UploadSession.objects.create(**defaults), True
    try:
        session, created = _with_db_retry(create_session)
    except IntegrityError:
        if not idempotency_key:
            raise
        session = _with_db_retry(lambda: UploadSession.objects.get(start_idempotency_key=idempotency_key))
        created = False
    if not created:
        if session.status not in {"pending", "finalizing", "completed"}:
            raise ValidationError("原上传会话状态不可恢复，请重新提交")
        if session.expected_photo_count != expected_photo_count or session.expected_document_count != expected_document_count:
            raise ValidationError("上传会话与当前文件清单不一致，请重新选择文件")
        if (
            session.access_token_hash != hashlib.sha256(token.encode()).hexdigest()
            or session.roster_sha256 != roster_sha256
            or session.roster_original_name != roster_original_name
            or session.roster_fingerprint != str(roster_fingerprint or "")[:255]
            or session.note != note[:500]
            or session.photo_manifest_digest != photo_manifest_digest
            or session.document_manifest_digest != document_manifest_digest
        ):
            raise ValidationError("上传会话与当前文件清单不一致，请重新选择文件")
        if not session.roster_file.name:
            stored_roster = default_storage.save(f"upload_sessions/{session.id}/roster/{roster_original_name}", roster_upload)
            _with_db_retry(lambda: UploadSession.objects.filter(pk=session.pk).update(roster_file=stored_roster))
        return session, token, session.expected_student_count
    stored_names = []
    try:
        roster_name = default_storage.save(f"upload_sessions/{session.id}/roster/{roster_original_name}", roster_upload)
        stored_names.append(roster_name)
        _with_db_retry(lambda: UploadSession.objects.filter(pk=session.pk).update(roster_file=roster_name))
        if documents:
            stage_upload_batch(
                session.id,
                "document",
                0,
                documents,
                manifest_digest=session.photo_manifest_digest,
                roster_fingerprint=session.roster_fingerprint,
                trusted_internal=True,
                already_prepared=True,
            )
        return session, token, len(records)
    except Exception:
        _delete_stored_files(stored_names)
        session.delete()
        raise


def stage_upload_batch(session_id, kind, sequence, uploads, *, token=None, manifest_digest=None, roster_fingerprint=None, trusted_internal=False, already_prepared=False):
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
    if not trusted_internal and not token:
        raise ValidationError("上传凭证不能为空")
    prepared = uploads if already_prepared else (_prepare_photos(uploads) if kind == "photo" else _prepare_documents(uploads))
    if sum(item.size for item in prepared) > settings.MAX_BATCH_UPLOAD_SIZE_MB * 1024 * 1024:
        raise ValidationError(f"本批文件总大小不能超过 {settings.MAX_BATCH_UPLOAD_SIZE_MB} MB")
    digest = _manifest_digest(prepared)
    session = _with_db_retry(lambda: UploadSession.objects.get(pk=session_id))
    if not trusted_internal:
        _check_upload_token(session, token)
    elif token is not None:
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
            if kind == "document" and locked_session.document_manifest:
                previous = list(UploadBatch.objects.filter(session=locked_session, kind="document").order_by("sequence"))
                if [item.sequence for item in previous] != list(range(sequence)):
                    raise ValidationError("文档批次必须按顺序上传，请从上一个成功批次继续")
                offset = sum(item.count for item in previous)
                expected = locked_session.document_manifest[offset:offset + len(prepared)]
                if len(expected) != len(prepared):
                    raise ValidationError("文档数量超过提交时的文档清单")
                for expected_item, actual_item in zip(expected, prepared):
                    if expected_item["name"] != actual_item.original_name or expected_item["size"] != actual_item.size or expected_item["sha256"] != actual_item.sha256:
                        raise ValidationError("文档名称、大小或顺序已变化，请重新选择文件")
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
            persisted = _with_db_retry(lambda: list(batch.uploads.order_by("ordinal")))
            if persisted and all(item.file.name != stored_names[index] for index, item in enumerate(persisted)):
                _delete_stored_files(stored_names)
        return batch, len(prepared)
    except Exception:
        _delete_stored_files(stored_names)
        raise


def upload_session_status(*, session_id, token, manifest_digest=None, roster_fingerprint=None):
    session = _with_db_retry(lambda: UploadSession.objects.get(pk=session_id))
    _check_upload_token(session, token)
    if session.photo_manifest_digest and manifest_digest and manifest_digest != session.photo_manifest_digest:
        raise ValidationError("照片清单已变化，请重新选择 Excel 和照片后提交")
    if session.roster_fingerprint and roster_fingerprint and roster_fingerprint != session.roster_fingerprint:
        raise ValidationError("Excel 文件已变化，请重新选择 Excel 和照片后提交")
    batches = []
    confirmed_photo_count = 0
    confirmed_document_count = 0
    for batch in UploadBatch.objects.filter(session=session).prefetch_related("uploads").order_by("kind", "sequence"):
        uploads = list(batch.uploads.all())
        item = {
            "kind": batch.kind,
            "sequence": batch.sequence,
            "count": batch.count,
            "digest": batch.digest,
            "uploads": [
                {"ordinal": row.ordinal, "name": row.original_name, "size": row.size, "sha256": row.sha256}
                for row in uploads
            ],
        }
        batches.append(item)
        if batch.kind == "photo":
            confirmed_photo_count += batch.count
        else:
            confirmed_document_count += batch.count
    completed_submission_id = str(session.completed_submission_id) if session.completed_submission_id else ""
    return {
        "upload_id": str(session.id),
        "status": session.status,
        "expected_student_count": session.expected_student_count,
        "expected_photo_count": session.expected_photo_count,
        "expected_document_count": session.expected_document_count,
        "confirmed_photo_count": confirmed_photo_count,
        "confirmed_document_count": confirmed_document_count,
        "photo_manifest": session.photo_manifest,
        "photo_manifest_digest": session.photo_manifest_digest,
        "document_manifest": session.document_manifest,
        "document_manifest_digest": session.document_manifest_digest,
        "roster_fingerprint": session.roster_fingerprint,
        "batches": batches,
        "completed_submission_id": completed_submission_id,
        "result_token": _session_result_token(session.id) if completed_submission_id else "",
    }


def _check_submission_token(submission, token):
    if not token or hashlib.sha256(token.encode()).hexdigest() != submission.access_token_hash:
        raise ValidationError("访问凭证无效")


def create_photo_supplement(*, submission_id, token, photo_uploads, note="", ip_address=None):
    submission = _with_db_retry(lambda: Submission.objects.prefetch_related("students").get(pk=submission_id))
    _check_submission_token(submission, token)
    if submission.status == "rejected":
        raise ValidationError("退回的提交版本不能补传照片")
    if not photo_uploads:
        raise ValidationError("请选择需要补传的照片")
    if len(photo_uploads) > settings.MAX_PHOTOS_PER_BATCH:
        raise ValidationError(f"每次最多补传 {settings.MAX_PHOTOS_PER_BATCH} 张照片")
    prepared = _prepare_photos(photo_uploads)
    _check_storage_quota(sum(item.size for item in prepared))

    def create_rows():
        with transaction.atomic():
            locked = Submission.objects.select_for_update().get(pk=submission.pk)
            if PhotoSupplement.objects.filter(submission=locked, status="pending").exists():
                raise ValidationError("已有待管理员确认的补传，请等待审核")
            students = list(Student.objects.filter(submission=locked).order_by("id"))
            existing_names = set(StudentPhoto.objects.filter(submission=locked).values_list("student_id", flat=True))
            by_name = defaultdict(list)
            for student in students:
                by_name[student.name].append(student)
            matches = []
            seen_students = set()
            for item in prepared:
                stem = PurePosixPath(item.original_name).stem
                candidates = [student for student in by_name.get(stem, []) if student.id not in existing_names and student.id not in seen_students]
                student = candidates[0] if len(candidates) == 1 else None
                if student:
                    seen_students.add(student.id)
                matches.append(student)
            supplement = PhotoSupplement.objects.create(submission=locked, note=note[:500])
            return supplement, matches

    supplement, matches = _with_db_retry(create_rows)
    stored_names = []
    try:
        rows = []
        for item, student in zip(prepared, matches):
            stored = _store_prepared_upload(item, f"supplements/{supplement.id}/photos/{item.original_name}")
            stored_names.append(stored)
            rows.append(SupplementPhoto(
                supplement=supplement, student=student, original_name=item.original_name, file=stored,
                content_type=item.content_type, size=item.size, sha256=item.sha256,
                match_status="matched" if student else "ambiguous",
            ))
        SupplementPhoto.objects.bulk_create(rows)
        AuditLog.objects.create(actor=None, action="photo_supplement_created", target_type="Submission", target_id=str(submission.id), detail={"supplement_id": str(supplement.id), "photo_count": len(rows)}, ip_address=ip_address)
        return supplement, sum(bool(student) for student in matches), len(matches) - sum(bool(student) for student in matches)
    except Exception:
        _delete_stored_files(stored_names)
        supplement.delete()
        raise


def match_supplement_photo(*, photo_id, student_id=None, actor=None, ip_address=None):
    """Bind, change, or clear one pending supplement photo without changing its file."""
    def operation():
        with transaction.atomic():
            photo = SupplementPhoto.objects.select_related("supplement").get(pk=photo_id)
            supplement = PhotoSupplement.objects.select_for_update().get(pk=photo.supplement_id)
            if supplement.status != "pending":
                raise ValidationError("补传已经审核，不能再修改绑定")
            photo = SupplementPhoto.objects.select_for_update().get(pk=photo_id)
            previous_student_id = photo.student_id
            student = None
            if student_id:
                try:
                    student = Student.objects.get(pk=student_id, submission_id=supplement.submission_id)
                except Student.DoesNotExist as exc:
                    raise ValidationError("只能绑定当前提交版本中的学生") from exc
                if StudentPhoto.objects.filter(student_id=student.id).exists():
                    raise ValidationError("该学生已有照片，不能覆盖原照片")
                if SupplementPhoto.objects.filter(supplement=supplement, student_id=student.id).exclude(pk=photo.pk).exists():
                    raise ValidationError("同一补传中已有其他照片绑定该学生")
                photo.student = student
                photo.match_status = "matched"
            else:
                photo.student = None
                photo.match_status = "ambiguous"
            photo.save(update_fields=["student", "match_status"])
            AuditLog.objects.create(
                actor=actor,
                action="manual_supplement_photo_match" if student else "manual_supplement_photo_unmatch",
                target_type="SupplementPhoto",
                target_id=str(photo.id),
                detail={"supplement_id": str(supplement.id), "previous_student_id": previous_student_id, "student_id": photo.student_id},
                ip_address=ip_address,
            )
            return photo

    return _with_db_retry(operation)


def review_photo_supplement(*, supplement_id, action, actor, ip_address=None):
    if action not in {"approved", "rejected"}:
        raise ValidationError("补传审核操作不正确")

    def operation():
        with transaction.atomic():
            supplement = PhotoSupplement.objects.select_for_update().select_related("submission").get(pk=supplement_id)
            if supplement.status != "pending":
                raise ValidationError("补传已审核")
            submission = Submission.objects.select_for_update().get(pk=supplement.submission_id)
            if action == "approved":
                current = Submission.objects.filter(class_group_id=submission.class_group_id, status="approved").first()
                if submission.status != "approved" or current is None or current.pk != submission.pk:
                    supplement.status = "expired"
                    supplement.reviewed_at = timezone.now()
                    supplement.reviewed_by = actor
                    supplement.save(update_fields=["status", "reviewed_at", "reviewed_by"])
                    AuditLog.objects.create(actor=actor, action="photo_supplement_expired", target_type="Submission", target_id=str(submission.id), detail={"supplement_id": str(supplement.id), "reason": "原提交版本已失效"}, ip_address=ip_address)
                    return supplement
            photos = list(supplement.photos.select_related("student"))
            if action == "approved":
                if any(photo.match_status != "matched" or photo.student_id is None for photo in photos):
                    raise ValidationError("仍有照片无法唯一匹配，请先人工绑定")
                student_ids = [photo.student_id for photo in photos]
                if len(student_ids) != len(set(student_ids)):
                    raise ValidationError("同一补传中不能有两张照片绑定同一学生，请先纠正绑定")
                if any(photo.student is None or photo.student.submission_id != submission.id for photo in photos):
                    raise ValidationError("照片不能跨提交版本或班级绑定")
                for photo in photos:
                    if StudentPhoto.objects.filter(student_id=photo.student_id).exists():
                        raise ValidationError("补传照片将覆盖已有照片，操作已拒绝")
                for photo in photos:
                    StudentPhoto.objects.create(student=photo.student, submission=submission, original_name=photo.original_name, file=photo.file.name, content_type=photo.content_type, size=photo.size, sha256=photo.sha256)
                    photo.student.match_status = "matched"
                    photo.student.save(update_fields=["match_status"])
                supplement.status = "approved"
            else:
                supplement.status = "rejected"
            supplement.reviewed_at = timezone.now()
            supplement.reviewed_by = actor
            supplement.save(update_fields=["status", "reviewed_at", "reviewed_by"])
            AuditLog.objects.create(actor=actor, action=f"photo_supplement_{action}", target_type="Submission", target_id=str(supplement.submission_id), detail={"supplement_id": str(supplement.id), "photo_count": len(photos)}, ip_address=ip_address)
            return supplement

    result = _with_db_retry(operation)
    if result.status in {"approved", "expired"}:
        # Approved files are referenced by StudentPhoto; expired files remain
        # available for audit and must not be mistaken for an active version.
        return result
    rejected_files = list(result.photos.values_list("file", flat=True))
    _delete_stored_files(rejected_files)
    result.photos.all().delete()
    return result


def _mark_upload_session_completed(session_id, submission):
    with transaction.atomic():
        locked = UploadSession.objects.select_for_update().get(pk=session_id)
        locked.status = "completed"
        locked.completed_submission = submission
        locked.save(update_fields=["status", "completed_submission", "updated_at"])


def finalize_upload_session(*, session_id, token, manifest_digest=None, roster_fingerprint=None, ip_address=None):
    session = _with_db_retry(lambda: UploadSession.objects.get(pk=session_id))
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
            if locked.status == "finalizing":
                stable_token = _session_result_token(session_id)
                recovered = Submission.objects.filter(access_token_hash=hashlib.sha256(stable_token.encode()).hexdigest()).first()
                stale_at = timezone.now() - timedelta(seconds=settings.UPLOAD_FINALIZING_TIMEOUT_SECONDS)
                if recovered is not None and _submission_is_complete(
                    recovered,
                    expected_student_count=locked.expected_student_count or None,
                    expected_photo_count=locked.expected_photo_count,
                    expected_document_count=locked.expected_document_count,
                ):
                    locked.status = "completed"
                    locked.completed_submission = recovered
                    locked.save(update_fields=["status", "completed_submission", "updated_at"])
                    return recovered, False
                if locked.updated_at > stale_at:
                    raise ValidationError("上传会话正在完成，请稍后重试")
                locked.status = "pending"
                locked.save(update_fields=["status", "updated_at"])
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
            expected_student_count=len(records),
            expected_photo_count=session.expected_photo_count,
            expected_document_count=session.expected_document_count,
        )
        _with_db_retry(lambda: _mark_upload_session_completed(session.id, submission))
        _delete_stored_files([roster_path] + [row.file.name for row in photo_rows + document_rows])
        return submission, result_token
    except Exception:
        _with_db_retry(lambda: UploadSession.objects.filter(pk=session.id, status="finalizing").update(status="pending"))
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


class ExportFileError(OSError):
    """A source file could not be copied completely into the export staging area."""


def _stage_export_file(file_field, *, expected_size=None, expected_sha256=""):
    """Read a source fully before it is allowed to become a ZIP member."""
    Path(settings.EXPORT_TEMP_DIR).mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="student-export-source-", dir=settings.EXPORT_TEMP_DIR)
    os.close(fd)
    temp_path = Path(temp_name)
    total = 0
    digest = hashlib.sha256()
    try:
        with file_field.open("rb") as source, temp_path.open("wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                digest.update(chunk)
                total += len(chunk)
        if expected_size is not None and total != expected_size:
            raise ExportFileError(f"文件大小不一致：期望 {expected_size}，实际 {total}")
        if expected_sha256 and not hmac.compare_digest(digest.hexdigest(), expected_sha256):
            raise ExportFileError("文件 SHA256 不一致")
        return temp_path
    except (FileNotFoundError, OSError, ValueError) as exc:
        temp_path.unlink(missing_ok=True)
        if isinstance(exc, ExportFileError):
            raise
        raise ExportFileError(str(exc)) from exc


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
                        staged_roster = None
                        try:
                            staged_roster = _stage_export_file(submission.roster_file, expected_size=submission.roster_file.size)
                        except (ExportFileError, FileNotFoundError, OSError, ValueError) as exc:
                            class_file_anomalies.append(("文件缺失", "学生名单", f"{roster_name}：{exc}"))
                        else:
                            # Do not create the ZIP member until the complete source is staged.
                            archive.write(staged_roster, f"{class_dir}/学生名单/{roster_name}")
                        finally:
                            if staged_roster is not None:
                                staged_roster.unlink(missing_ok=True)

                document_names = set()
                for document in SubmissionDocument.objects.filter(submission__in=class_submissions).distinct():
                    document_name = safe_filename(document.original_name)
                    if document_name in document_names:
                        stem = PurePosixPath(document_name).stem
                        suffix = PurePosixPath(document_name).suffix
                        document_name = f"{str(document.id)}_{stem}{suffix}"
                    document_names.add(document_name)
                    staged_document = None
                    try:
                        staged_document = _stage_export_file(document.file, expected_size=document.size)
                    except (ExportFileError, FileNotFoundError, OSError, ValueError) as exc:
                        class_file_anomalies.append(("文件缺失", "相关文档", f"{document_name}：{exc}"))
                    else:
                        # Archive I/O errors intentionally escape and roll back the whole temporary ZIP.
                        archive.write(staged_document, f"{class_dir}/相关文档/{document_name}")
                    finally:
                        if staged_document is not None:
                            staged_document.unlink(missing_ok=True)

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
                            staged_photo = None
                            try:
                                staged_photo = _stage_export_file(photo.file, expected_size=photo.size, expected_sha256=photo.sha256)
                            except ExportFileError as exc:
                                student._export_status = "文件缺失"
                                photo_anomalies.append(("文件缺失", student.name, f"照片无法读取：{photo.file.name}；{exc}"))
                            else:
                                # Only write a ZIP member after the entire source has been read and verified.
                                # An archive I/O error aborts and removes the whole temporary ZIP; it must not
                                # be downgraded to a successful-looking truncated member.
                                archive.write(staged_photo, f"{category_dir}/学生照片/{candidate_name}")
                                student._export_name = candidate_name
                                student._export_status = "已导出"
                            finally:
                                if staged_photo is not None:
                                    staged_photo.unlink(missing_ok=True)
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
    supplements = list(PhotoSupplement.objects.prefetch_related("photos"))
    supplement_photos = [photo for supplement in supplements for photo in supplement.photos.all()]
    documents = list(SubmissionDocument.objects.all())
    submissions = list(Submission.objects.all())
    staged_uploads = list(StagedUpload.objects.all())
    upload_sessions = list(UploadSession.objects.all())
    roster_count = sum(bool(submission.roster_file) for submission in submissions)
    for obj in photos + documents:
        obj.file.delete(save=False)
    for obj in supplement_photos:
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
        "supplements": len(supplements),
        "supplement_photos": len(supplement_photos),
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
    supplements_root = Path(settings.MEDIA_ROOT) / "supplements"
    shutil.rmtree(supplements_root, ignore_errors=True)
    return counts


def template_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "学生名单"
    last_column = get_column_letter(len(HEADERS))
    sheet.merge_cells(f"A1:{last_column}1")
    sheet["A1"] = "填写说明：一行一位学生；姓名、学部、学年、年级、班级、就读类型为必填；学年是当前在读学年的起始年份，不是入学年份。"
    sheet.merge_cells(f"A2:{last_column}2")
    sheet["A2"] = "同一学年可填写初一、初二、初三、高一、高二或高三；旧版没有年级列的名单仍按旧学年映射兼容；照片默认按姓名匹配。"
    for cell_ref in ("A1", "A2"):
        sheet[cell_ref].font = Font(bold=True, color="FFFFFF" if cell_ref == "A1" else "17233B")
        sheet[cell_ref].fill = PatternFill("solid", fgColor="17233B" if cell_ref == "A1" else "E8F5F2")
        sheet[cell_ref].alignment = Alignment(vertical="center")
    for col, header in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=4, column=col, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="E76F3C")
    for col, value in enumerate(["示例同学", "男", "高中部", "2026", "高一", "1", "走读", "虚构示例，仅用于测试"], start=1):
        sheet.cell(row=5, column=col, value=value)
    sheet.freeze_panes = "A5"
    for col in range(1, len(HEADERS) + 1): sheet.column_dimensions[get_column_letter(col)].width = 18
    out = io.BytesIO(); book.save(out); return out.getvalue()

