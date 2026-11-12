"""Upload preflight checks.

This module deliberately has no view or URL dependency.  It can be called by
the public upload flow before a session is started, while the final upload
services remain the authoritative validation boundary.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Iterable

from openpyxl import load_workbook
from PIL import Image, UnidentifiedImageError

from .photo_quality import PhotoQualityPolicy, assess_photo_quality, quality_issue_level


DEPARTMENTS = {"初中部", "高中部"}
GRADES = {"初一", "初二", "初三", "高一", "高二", "高三"}
STUDY_TYPES = {"走读", "半走读", "住宿"}
YEARS = {"2024", "2025", "2026"}
GRADE_BY_YEAR = {
    "2024": {"初中部": "初三", "高中部": "高三"},
    "2025": {"初中部": "初二", "高中部": "高二"},
    "2026": {"初中部": "初一", "高中部": "高一"},
}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png"}
REQUIRED_HEADERS = ("姓名", "学部", "学年", "班级", "就读类型")


@dataclass(frozen=True)
class PreflightLimits:
    """Limits used by preflight, overridable for isolated tests."""

    max_photo_count: int = 300
    max_photo_bytes: int = 20 * 1024 * 1024
    max_total_photo_bytes: int = 300 * 20 * 1024 * 1024

    @classmethod
    def from_settings(cls) -> "PreflightLimits":
        try:
            from django.conf import settings

            megabyte = 1024 * 1024
            per_file = int(settings.MAX_UPLOAD_SIZE_MB) * megabyte
            return cls(
                max_photo_count=int(settings.MAX_PHOTOS_PER_SUBMISSION),
                max_photo_bytes=per_file,
                max_total_photo_bytes=int(settings.MAX_PHOTOS_PER_SUBMISSION) * per_file,
            )
        except Exception:
            return cls()


@dataclass(frozen=True)
class PreflightIssue:
    code: str
    message: str
    level: str
    row: int | None = None
    filename: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "level": self.level,
            "row": self.row,
            "filename": self.filename,
        }


@dataclass(frozen=True)
class PreflightStudent:
    row: int
    name: str
    gender: str
    department: str
    school_year: str
    grade: str
    class_name: str
    study_type: str

    @property
    def scope(self) -> tuple[str, str, str, str]:
        return (self.school_year, self.department, self.grade, self.class_name)


@dataclass(frozen=True)
class PreflightPhoto:
    filename: str
    size: int
    sha256: str = ""
    status: str = "invalid"
    quality_status: str = "not_checked"
    quality_code: str = ""
    quality_message: str = ""
    quality: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightResult:
    records: list[PreflightStudent] = field(default_factory=list)
    photos: list[PreflightPhoto] = field(default_factory=list)
    blocking_errors: list[PreflightIssue] = field(default_factory=list)
    warnings: list[PreflightIssue] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        matched = sum(photo.status == "matched" for photo in self.photos)
        manual = sum(photo.status == "manual" for photo in self.photos)
        missing = 0
        for student in self.records:
            if any(
                photo.status == "matched" and _photo_stem(photo.filename) == _name_key(student.name)
                for photo in self.photos
            ):
                continue
            missing += 1
        return {
            "student_count": len(self.records),
            "photo_count": len(self.photos),
            "matched_count": matched,
            "missing_count": missing,
            "manual_count": manual,
        }

    def can_continue(self, warnings_acknowledged: bool = False) -> bool:
        return not self.blocking_errors and (warnings_acknowledged or not self.warnings)

    @property
    def has_blocking_errors(self) -> bool:
        return bool(self.blocking_errors)

    def as_dict(self) -> dict[str, Any]:
        summary = {
            **self.summary,
            "quality_failed_count": sum(photo.quality_status == "failed" for photo in self.photos),
            "quality_passed_count": sum(photo.quality_status == "passed" for photo in self.photos),
        }
        return {
            "summary": summary,
            "records": [record.__dict__ for record in self.records],
            "photos": [photo.__dict__ for photo in self.photos],
            "blocking_errors": [issue.as_dict() for issue in self.blocking_errors],
            "warnings": [issue.as_dict() for issue in self.warnings],
            "can_continue_without_warning_ack": self.can_continue(),
            "can_continue_after_warning_ack": self.can_continue(warnings_acknowledged=True),
        }


def run_preflight(
    roster_upload: Any,
    photo_uploads: Iterable[Any] | None = None,
    *,
    limits: PreflightLimits | None = None,
    quality_policy: PhotoQualityPolicy | None = None,
) -> PreflightResult:
    """Validate a roster and selected photos without creating upload state.

    ``photo_uploads`` accepts Django UploadedFile objects or seekable binary
    file-like objects with a ``name`` attribute.  Every file is rewound before
    returning so the final upload path can read it again.
    """

    result = PreflightResult()
    limits = limits or PreflightLimits.from_settings()
    result.records = _read_roster(roster_upload, result)
    # The HTTP view supplies the database policy explicitly. Keeping the pure
    # function default off also makes non-HTTP callers safe and predictable.
    result.photos = _read_photos(photo_uploads or [], result, limits, quality_policy or PhotoQualityPolicy())
    _resolve_photo_matches(result)
    return result


def _add_issue(result: PreflightResult, *, code: str, message: str, level: str, row: int | None = None, filename: str = "") -> None:
    issue = PreflightIssue(code=code, message=message, level=level, row=row, filename=filename)
    (result.blocking_errors if level == "error" else result.warnings).append(issue)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalise_year(value: str) -> str | None:
    match = re.fullmatch(r"(20\d{2})(?:[-/]20\d{2})?", value)
    year = match.group(1) if match else ""
    return year if year in YEARS else None


def _normalise_grade(value: str, department: str, year: str) -> str | None:
    if not value:
        return GRADE_BY_YEAR.get(year, {}).get(department)
    if value not in GRADES:
        return None
    if department == "初中部" and not value.startswith("初"):
        return None
    if department == "高中部" and not value.startswith("高"):
        return None
    return value


def _read_roster(upload: Any, result: PreflightResult) -> list[PreflightStudent]:
    filename = _filename(upload)
    if PurePosixPath(filename).suffix.lower() != ".xlsx":
        _add_issue(result, code="invalid_roster", message="Excel 名单必须是 .xlsx 文件", level="error", filename=filename)
        return []
    try:
        _rewind(upload)
        workbook = load_workbook(upload, read_only=True, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
    except Exception:
        _add_issue(result, code="invalid_roster", message="Excel 无法读取，请使用系统模板保存为 .xlsx", level="error", filename=filename)
        return []
    finally:
        _rewind(upload)

    if not rows:
        _add_issue(result, code="empty_roster", message="Excel 没有数据", level="error", filename=filename)
        return []
    header_index = next(
        (
            index
            for index, row in enumerate(rows[:10])
            if set(REQUIRED_HEADERS).issubset({_text(value) for value in row})
        ),
        None,
    )
    if header_index is None:
        _add_issue(result, code="missing_headers", message="Excel 缺少必填列：" + "、".join(REQUIRED_HEADERS), level="error", filename=filename)
        return []
    header = [_text(value) for value in rows[header_index]]
    positions = {name: index for index, name in enumerate(header) if name}
    records: list[PreflightStudent] = []
    exact_keys: set[tuple[str, ...]] = set()
    name_scopes: defaultdict[tuple[str, ...], list[int]] = defaultdict(list)
    scopes: set[tuple[str, str, str, str]] = set()

    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(value not in (None, "") for value in row):
            continue
        value = {
            field: _text(row[index]) if index < len(row) else ""
            for field, index in positions.items()
        }
        row_errors: list[tuple[str, str]] = []
        for field in REQUIRED_HEADERS:
            if not value.get(field):
                row_errors.append(("missing_required", f"第 {row_number} 行缺少{field}"))
        department = value.get("学部", "")
        if department and department not in DEPARTMENTS:
            row_errors.append(("invalid_department", f"第 {row_number} 行学部只能是初中部或高中部"))
        year = _normalise_year(value.get("学年", ""))
        if value.get("学年") and year is None:
            row_errors.append(("invalid_school_year", f"第 {row_number} 行学年只能填写 2024、2025 或 2026"))
        grade = _normalise_grade(value.get("年级", ""), department, year or "")
        if value.get("年级") and grade is None:
            row_errors.append(("invalid_grade", f"第 {row_number} 行年级与学部不匹配或格式不正确"))
        study_type = value.get("就读类型", "")
        if study_type and study_type not in STUDY_TYPES:
            row_errors.append(("invalid_study_type", f"第 {row_number} 行就读类型只能是走读、半走读或住宿"))
        gender = value.get("性别", "")
        if gender and gender not in {"男", "女"}:
            row_errors.append(("invalid_gender", f"第 {row_number} 行性别只能是男或女"))

        if row_errors:
            for code, message in row_errors:
                _add_issue(result, code=code, message=message, level="error", row=row_number)
            continue

        record = PreflightStudent(
            row=row_number,
            name=value["姓名"],
            gender=gender,
            department=department,
            school_year=year or "",
            grade=grade or "",
            class_name=value["班级"],
            study_type=study_type,
        )
        exact_key = (
            record.school_year,
            record.department,
            record.grade,
            record.class_name,
            record.name,
            record.gender,
            record.study_type,
        )
        if exact_key in exact_keys:
            _add_issue(result, code="duplicate_student_row", message=f"第 {row_number} 行与前面学生记录完全重复", level="error", row=row_number)
        exact_keys.add(exact_key)
        name_key = record.scope + (record.name.casefold(),)
        name_scopes[name_key].append(row_number)
        records.append(record)
        scopes.add(record.scope)

    if len(scopes) > 1:
        _add_issue(result, code="mixed_class_scope", message="一次预提交只能包含同一学年、学部、年级和班级", level="error")
    for key, row_numbers in name_scopes.items():
        if len(row_numbers) > 1:
            _add_issue(
                result,
                code="same_name_student",
                message=f"同一班级存在同名学生（第 {', '.join(map(str, row_numbers))} 行），照片不能按姓名自动绑定",
                level="warning",
                row=row_numbers[0],
            )
    return records


def _read_photos(
    uploads: Iterable[Any],
    result: PreflightResult,
    limits: PreflightLimits,
    quality_policy: PhotoQualityPolicy,
) -> list[PreflightPhoto]:
    uploads = list(uploads)
    if len(uploads) > limits.max_photo_count:
        _add_issue(result, code="photo_count_too_large", message=f"照片数量不能超过 {limits.max_photo_count} 张", level="error")
    filename_counts = Counter(_filename(upload).casefold() for upload in uploads)
    for filename, count in filename_counts.items():
        if filename and count > 1:
            _add_issue(result, code="duplicate_photo_filename", message=f"照片文件名重复：{filename}", level="error", filename=filename)

    total_size = 0
    checked: list[PreflightPhoto] = []
    for upload in uploads:
        filename = _filename(upload)
        size = _upload_size(upload)
        total_size += size
        status = "invalid"
        digest = ""
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix not in PHOTO_EXTENSIONS:
            _add_issue(result, code="unsupported_photo_type", message=f"照片 {filename} 不是 JPG/PNG", level="error", filename=filename)
        if size > limits.max_photo_bytes:
            _add_issue(result, code="photo_too_large", message=f"照片 {filename} 超过单文件大小限制", level="error", filename=filename)
        try:
            _rewind(upload)
            with Image.open(upload) as image:
                image.verify()
                if image.format not in {"JPEG", "PNG"}:
                    raise ValueError("unsupported image format")
            _rewind(upload)
            digest = _sha256(upload)
            status = "valid"
            quality = None
            if quality_policy.detection_enabled:
                quality = assess_photo_quality(upload)
                if quality.status != "passed":
                    level = "error" if quality_policy.blocking_enabled else "warning"
                    _add_issue(result, code=quality.code, message=f"照片 {filename}：{quality.message}", level=level, filename=filename)
        except (UnidentifiedImageError, OSError, ValueError, EOFError):
            _add_issue(result, code="invalid_photo", message=f"照片 {filename} 损坏或无法读取", level="error", filename=filename)
            quality = None
        finally:
            _rewind(upload)
        checked.append(PreflightPhoto(
            filename=filename,
            size=size,
            sha256=digest,
            status=status,
            quality_status=quality.status if quality else "not_checked",
            quality_code=quality.code if quality else "",
            quality_message=quality.message if quality else "",
            quality=quality.as_dict() if quality else {},
        ))
    if total_size > limits.max_total_photo_bytes:
        _add_issue(result, code="photo_total_too_large", message="照片总上传体积超过限制", level="error")
    return checked


def _resolve_photo_matches(result: PreflightResult) -> None:
    by_name: defaultdict[str, list[int]] = defaultdict(list)
    for index, student in enumerate(result.records):
        by_name[_name_key(student.name)].append(index)
    for index, photo in enumerate(result.photos):
        if photo.status != "valid":
            continue
        candidates = by_name.get(_photo_stem(photo.filename), [])
        if len(candidates) == 1:
            result.photos[index] = replace(photo, status="matched")
        elif len(candidates) > 1:
            result.photos[index] = replace(photo, status="manual")
            _add_issue(result, code="same_name_photo", message=f"照片 {photo.filename} 对应多个同名学生，需人工确认", level="warning", filename=photo.filename)
        else:
            result.photos[index] = replace(photo, status="manual")
            _add_issue(result, code="unmatched_photo", message=f"照片 {photo.filename} 无法按姓名匹配学生，需人工确认", level="warning", filename=photo.filename)


def _filename(upload: Any) -> str:
    raw = str(getattr(upload, "name", "") or "")
    return PurePosixPath(raw.replace("\\", "/")).name


def _rewind(upload: Any) -> None:
    if hasattr(upload, "seek"):
        upload.seek(0)


def _upload_size(upload: Any) -> int:
    size = getattr(upload, "size", None)
    if size is not None:
        try:
            return int(size)
        except (TypeError, ValueError):
            pass
    _rewind(upload)
    data = upload.read()
    _rewind(upload)
    return len(data)


def _sha256(upload: Any) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = upload.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _name_key(value: str) -> str:
    return value.strip().casefold()


def _photo_stem(filename: str) -> str:
    return _name_key(PurePosixPath(filename).stem)
