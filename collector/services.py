import hashlib
import io
import re
import secrets
import zipfile
from collections import Counter, defaultdict
from pathlib import PurePosixPath

from django.conf import settings
from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from PIL import Image, UnidentifiedImageError

from .models import AppSetting, AuditLog, ClassGroup, NamingRule, SchoolYear, Student, StudentPhoto, Submission, SubmissionDocument

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
ALLOWED_TOKENS = {"{姓名}", "{学生编号}", "{性别编号}", "{学部}", "{年级}", "{班级}", "{就读类型}"}
FIELD_MAP = {
    "姓名": "name", "学生编号": "student_no", "性别": "gender", "学部": "department",
    "学年": "school_year", "年级": "grade", "班级": "class_name", "就读类型": "study_type",
}
HEADERS = ["学生编号", "姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "照片文件名", "备注"]


class ValidationError(Exception):
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


def safe_filename(value, fallback="未命名"):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip().strip(".")
    return (value or fallback)[:160]


def safe_excel_value(value):
    value = "" if value is None else str(value)
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


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
    header = [_text(v) for v in rows[0]]
    missing = [h for h in ["姓名", "学部", "学年", "年级", "班级", "就读类型"] if h not in header]
    if missing:
        raise ValidationError("Excel 缺少必填列：" + "、".join(missing))
    positions = {h: i for i, h in enumerate(header) if h}
    records, errors = [], []
    seen = set()
    for row_no, row in enumerate(rows[1:], start=2):
        if not any(v not in (None, "") for v in row):
            continue
        record = {h: _text(row[i]) if i < len(row) else "" for h, i in positions.items()}
        row_errors = []
        for field in ["姓名", "学部", "学年", "年级", "班级", "就读类型"]:
            if not record.get(field):
                row_errors.append(f"缺少{field}")
        if record.get("性别") and record["性别"] not in {"男", "女"}:
            row_errors.append("性别只能是男或女")
        if record.get("学部") not in {"初中部", "高中部"}:
            row_errors.append("学部只能是初中部或高中部")
        if record.get("就读类型") not in {"走读", "非走读", "半走读"}:
            row_errors.append("就读类型不正确")
        identity = (record.get("学年"), record.get("学部"), record.get("年级"), record.get("班级"), record.get("学生编号") or record.get("姓名"))
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
    school_year, _ = SchoolYear.objects.get_or_create(name=record["学年"], defaults={"is_active": True})
    class_group, _ = ClassGroup.objects.get_or_create(
        department=department, school_year=school_year, grade=record["年级"], name=record["班级"], defaults={"is_active": True}
    )
    return school_year, class_group


def _match_photos(records, photo_uploads):
    by_name = defaultdict(list)
    for upload in photo_uploads:
        by_name[upload.name].append(upload)
        by_name[PurePosixPath(upload.name).stem].append(upload)
    by_no = defaultdict(list)
    by_student_name = defaultdict(list)
    for record in records:
        if record.get("学生编号"):
            by_no[record["学生编号"]].append(record)
        by_student_name[record["姓名"]].append(record)
    matches = {}
    used = set()
    for index, record in enumerate(records):
        candidates = []
        hint = record.get("照片文件名")
        explicit = bool(hint)
        if hint:
            candidates = by_name.get(hint, []) or by_name.get(PurePosixPath(hint).stem, [])
        if not candidates and record.get("学生编号"):
            candidates = [u for u in photo_uploads if PurePosixPath(u.name).stem == record["学生编号"]]
            explicit = explicit or bool(candidates)
        if not explicit and len(by_student_name[record["姓名"]]) > 1:
            matches[index] = "ambiguous"
            continue
        if not candidates and len(by_student_name[record["姓名"]]) == 1:
            candidates = by_name.get(record["姓名"], []) or by_name.get(PurePosixPath(record["姓名"]).stem, [])
        candidates = [u for u in candidates if id(u) not in used]
        matches[index] = candidates[0] if len(candidates) == 1 else ("ambiguous" if len(candidates) > 1 else None)
        if candidates and len(candidates) == 1:
            used.add(id(candidates[0]))
    return matches


@transaction.atomic
def create_submission(*, roster_upload, photo_uploads, document_uploads, note="", ip_address=None):
    records = parse_roster(roster_upload)
    scopes = {(r.get("学部"), r.get("学年"), r.get("年级"), r.get("班级")) for r in records}
    if len(scopes) != 1:
        raise ValidationError("一次提交只能包含同一学部、学年、年级和班级的学生")
    if AppSetting.get_value("collection_scope", "all") == "configured":
        missing_scopes = []
        for department_name, year_name, grade, class_name in scopes:
            department = "middle" if department_name == "初中部" else "high"
            if not ClassGroup.objects.filter(department=department, school_year__name=year_name, grade=grade, name=class_name, is_active=True).exists():
                missing_scopes.append(f"{department_name}/{year_name}/{grade}/{class_name}")
        if missing_scopes:
            raise ValidationError("当前仅允许已配置班级提交：" + "、".join(missing_scopes))
    if len(photo_uploads) > settings.MAX_PHOTOS_PER_SUBMISSION:
        raise ValidationError(f"照片数量不能超过 {settings.MAX_PHOTOS_PER_SUBMISSION} 张")
    if len(document_uploads) > settings.MAX_DOCUMENTS_PER_SUBMISSION:
        raise ValidationError(f"文档数量不能超过 {settings.MAX_DOCUMENTS_PER_SUBMISSION} 个")
    validated_photos = []
    for upload in photo_uploads:
        content_type, sha256 = image_bytes_info(upload)
        validated_photos.append((upload, content_type, sha256))
    for upload in document_uploads:
        document_info(upload)
    token = Submission.make_token()
    submission = Submission.objects.create(
        access_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        department="middle" if records[0]["学部"] == "初中部" else "high",
        school_year=_class_from_record(records[0])[0],
        grade=records[0]["年级"],
        class_group=_class_from_record(records[0])[1],
        source_filename=safe_filename(roster_upload.name), note=note[:500],
    )
    matches = _match_photos(records, photo_uploads)
    matched_upload_ids = set()
    for index, record in enumerate(records):
        school_year, class_group = _class_from_record(record)
        status = "matched" if hasattr(matches[index], "name") else ("ambiguous" if matches[index] == "ambiguous" else "missing")
        duplicate = Student.objects.filter(school_year=school_year, department=class_group.department, grade=record["年级"], class_name=record["班级"], name=record["姓名"], submission__status="approved").exists()
        student = Student.objects.create(
            submission=submission, student_no=record.get("学生编号", ""), name=record["姓名"], gender=record.get("性别", ""),
            department=class_group.department, school_year=school_year, grade=record["年级"], class_name=record["班级"],
            study_type=record["就读类型"], photo_filename_hint=record.get("照片文件名", ""), remark=record.get("备注", ""),
            match_status=status, is_duplicate=duplicate,
        )
        matched = matches[index]
        if hasattr(matched, "name"):
            upload, content_type, sha256 = next(p for p in validated_photos if p[0] is matched)
            StudentPhoto.objects.create(student=student, submission=submission, original_name=safe_filename(upload.name), file=upload, content_type=content_type, size=upload.size, sha256=sha256)
            matched_upload_ids.add(id(upload))
    for upload, content_type, sha256 in validated_photos:
        if id(upload) not in matched_upload_ids:
            StudentPhoto.objects.create(student=None, submission=submission, original_name=safe_filename(upload.name), file=upload, content_type=content_type, size=upload.size, sha256=sha256)
    for upload in document_uploads:
        SubmissionDocument.objects.create(submission=submission, original_name=safe_filename(upload.name), file=upload, content_type="application/octet-stream", size=upload.size)
    AuditLog.objects.create(action="public_submit", target_type="Submission", target_id=str(submission.id), detail={"student_count": len(records)}, ip_address=ip_address)
    return submission, token


def validate_naming_rule(rule):
    if rule.mode == "name":
        rule.template = "{姓名}"
    elif rule.mode == "gender_no":
        rule.template = "{姓名}" + rule.separator + "{性别编号}"
    tokens = set(re.findall(r"\{[^{}]+\}", rule.template))
    if not tokens.issubset(ALLOWED_TOKENS):
        raise ValidationError("命名模板包含不支持的字段")
    if any(part in rule.template for part in ["/", "\\", ".."]):
        raise ValidationError("命名模板不能包含路径片段")
    return rule


def render_filename(student, rule, used):
    values = {
        "{姓名}": student.name,
        "{学生编号}": student.student_no or "无编号",
        "{性别编号}": rule.male_code if student.gender == "男" else rule.female_code,
        "{学部}": _department_code(student.department),
        "{年级}": student.grade,
        "{班级}": student.class_name,
        "{就读类型}": student.study_type,
    }
    base = safe_filename(rule.template)
    for token, value in values.items():
        base = base.replace(token, safe_filename(value))
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
        ("非走读人数", sum(s.study_type == "非走读" for s in students)), ("半走读人数", sum(s.study_type == "半走读" for s in students)),
        ("成功导出照片数", sum(bool(getattr(s, "_export_name", "")) for s in students)),
        ("缺失照片数", sum(not bool(getattr(s, "_export_name", "")) for s in students)),
    ]
    for row in summary_rows: summary.append([safe_excel_value(value) for value in row])
    detail = book.create_sheet("学生明细")
    detail.append(["学生编号", "姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "实际导出照片文件名", "匹配状态"])
    for student in students:
        detail.append([safe_excel_value(value) for value in [student.student_no, student.name, student.gender, _department_code(student.department), student.school_year.name, student.grade, student.class_name, student.study_type, getattr(student, "_export_name", ""), student.get_match_status_display()]])
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


def export_students(filters, actor=None, ip_address=None):
    qs = Student.objects.select_related("submission", "school_year").filter(submission__status="approved")
    for key in ["school_year__name", "department", "grade", "class_name", "gender", "study_type"]:
        if filters.get(key): qs = qs.filter(**{key: filters[key]})
    students = list(qs.order_by("department", "grade", "class_name", "student_no", "name"))
    if filters.get("has_photo") == "yes": students = [s for s in students if s.match_status == "matched" and hasattr(s, "photo")]
    if filters.get("has_photo") == "no": students = [s for s in students if not (s.match_status == "matched" and hasattr(s, "photo"))]
    rule = validate_naming_rule(NamingRule.current())
    groups = defaultdict(list)
    for student in students: groups[(student.department, student.grade, student.class_name)].append(student)
    root = io.BytesIO()
    anomalies_total = []
    with zipfile.ZipFile(root, "w", zipfile.ZIP_DEFLATED) as archive:
        for group_key, group_students in groups.items():
            used, anomalies = set(), []
            dept, grade, class_name = group_key
            class_dir = "/".join(safe_filename(v) for v in [_department_code(dept), grade, class_name])
            for student in group_students:
                student._export_name = ""
                if student.match_status == "matched" and hasattr(student, "photo"):
                    student._export_name = render_filename(student, rule, used)
                    with student.photo.file.open("rb") as source:
                        archive.writestr(f"{class_dir}/学生照片/{student._export_name}", source.read())
                else:
                    anomalies.append((student.get_match_status_display(), student.name, "没有可导出的已匹配照片"))
            for document in SubmissionDocument.objects.filter(submission__students__in=group_students).distinct():
                with document.file.open("rb") as source:
                    archive.writestr(f"{class_dir}/相关文档/{safe_filename(document.original_name)}", source.read())
            archive.writestr(f"{class_dir}/学生统计表.xlsx", _write_workbook(group_students, f"{class_dir}", anomalies))
            anomalies_total.extend(anomalies)
        if len(groups) > 1:
            for student in students: student._export_name = getattr(student, "_export_name", "")
            archive.writestr("总统计表.xlsx", _write_workbook(students, "跨班级导出", anomalies_total))
    AuditLog.objects.create(actor=actor, action="export_students", target_type="Student", detail={"filters": filters, "count": len(students)}, ip_address=ip_address)
    return root.getvalue(), len(students), [getattr(s, "_export_name", "") for s in students[:10]]


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
        submission.delete()
    AuditLog.objects.create(actor=actor, action="clear_rejected_submissions", detail={"submissions": len(submissions), "students": removed_students, "files": removed_files}, ip_address=ip_address)
    return len(submissions), removed_students, removed_files


def template_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "学生名单"
    sheet.append(HEADERS)
    sheet.append(["2026-001", "示例同学", "男", "高中部", "2026-2027", "高一", "1", "走读", "2026-001.jpg", "虚构示例，仅用于测试"])
    for col in range(1, len(HEADERS) + 1): sheet.column_dimensions[get_column_letter(col)].width = 18
    out = io.BytesIO(); book.save(out); return out.getvalue()

