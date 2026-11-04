"""Administrator collection statistics and exception workbook helpers.

This module deliberately has no URL or view dependency.  The integration line can
call ``build_admin_statistics_context`` from its protected admin view and render
``collector/admin_statistics.html`` without changing the existing export path.
"""

from collections import Counter
from io import BytesIO

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from .models import AppSetting, ClassGroup, PhotoSupplement, StudentPhoto, Submission
from .services import safe_excel_value


STATUSES = {
    "approved": "已采用",
    "pending": "待审核",
    "rejected": "已退回",
    "none": "未提交",
}
FILTER_KEYS = ("school_year", "department", "grade", "class_name", "status")


def _filters(filters=None):
    """Normalize a dict/QueryDict without allowing arbitrary ORM lookups."""
    filters = filters or {}
    result = {key: str(filters.get(key, "") or "").strip() for key in FILTER_KEYS}
    if result["department"] not in {"", "middle", "high"}:
        result["department"] = "__invalid__"
    if result["status"] not in {"", *STATUSES}:
        result["status"] = "__invalid__"
    return result


def _scope_key(group):
    return (group.school_year_id, group.department, group.grade, group.name)


def _matches_scope(group, values):
    return (
        (not values["school_year"] or group.school_year.name == values["school_year"])
        and (not values["department"] or group.department == values["department"])
        and (not values["grade"] or group.grade == values["grade"])
        and (not values["class_name"] or group.name == values["class_name"])
    )


def _scope_groups(values):
    """Return configured scopes plus any scope that has submitted data."""
    groups = {_scope_key(group): group for group in ClassGroup.objects.select_related("school_year").all()}
    submissions = Submission.objects.select_related("class_group", "school_year").all()
    for submission in submissions:
        group = submission.class_group
        groups.setdefault(_scope_key(group), group)
    return [group for group in groups.values() if _matches_scope(group, values)]


def _latest(items):
    return max(items, key=lambda item: (item.submitted_at, str(item.id))) if items else None


def _effective_submission(group):
    """Return the single current adopted version, even for legacy duplicate data."""
    return _latest(list(Submission.objects.filter(class_group=group, status="approved")))


def _scope_status(submissions, adopted):
    if adopted is not None:
        return "approved"
    if any(item.status == "pending" for item in submissions):
        return "pending"
    if any(item.status == "rejected" for item in submissions):
        return "rejected"
    return "none"


def _status_matches(effective_status, submissions, requested):
    if not requested:
        return True
    if requested == "approved":
        return effective_status == "approved"
    if requested == "pending":
        return any(item.status == "pending" for item in submissions)
    if requested == "rejected":
        return effective_status == "rejected"
    return effective_status == "none"


def _student_counts(submission):
    if submission is None:
        return [], {"student_count": 0, "matched_photo_count": 0, "missing_photo_count": 0, "ambiguous_count": 0, "duplicate_count": 0}
    students = list(submission.students.order_by("class_name", "name", "id"))
    photo_student_ids = set(
        StudentPhoto.objects.filter(submission=submission, student__isnull=False).values_list("student_id", flat=True)
    )
    matched = sum(student.match_status == "matched" and student.id in photo_student_ids for student in students)
    return students, {
        "student_count": len(students),
        "matched_photo_count": matched,
        "missing_photo_count": len(students) - matched,
        "ambiguous_count": sum(student.match_status == "ambiguous" for student in students),
        "duplicate_count": sum(student.is_duplicate for student in students),
    }


def _rate(matched, total):
    return round(matched * 100 / total, 2) if total else 0.0


def _coverage(total_adopted):
    """Use a configured expected count only; never call submitted count coverage."""
    raw = AppSetting.get_value("school_expected_student_count", "").strip()
    try:
        expected = int(raw)
    except (TypeError, ValueError):
        expected = 0
    if expected <= 0:
        return {
            "available": False,
            "expected_count": None,
            "covered_count": total_adopted,
            "rate": None,
            "reason": "未配置全校应采学生名册或应采人数，不能把已提交人数当作全校覆盖率。",
        }
    return {
        "available": True,
        "expected_count": expected,
        "covered_count": total_adopted,
        "rate": _rate(total_adopted, expected),
        "reason": "依据管理员配置的全校应采学生数计算。",
    }


def build_admin_statistics_context(filters=None):
    """Build the protected-admin statistics context.

    ``rows`` is one row per school-year/department/grade/class scope.  Student
    counts and completion rates are only from the current adopted submission.
    A pending replacement is reported separately and does not double-count the
    older adopted version.
    """
    values = _filters(filters)
    rows = []
    for group in _scope_groups(values):
        submissions = list(Submission.objects.filter(class_group=group).order_by("submitted_at", "id"))
        adopted = _effective_submission(group)
        effective_status = _scope_status(submissions, adopted)
        if not _status_matches(effective_status, submissions, values["status"]):
            continue
        students, counts = _student_counts(adopted)
        pending_supplements = (
            PhotoSupplement.objects.filter(submission=adopted, status="pending").count() if adopted is not None else 0
        )
        rows.append(
            {
                "school_year": group.school_year.name,
                "department": group.department,
                "department_label": group.get_department_display(),
                "grade": group.grade,
                "class_name": group.name,
                "scope_label": f"{group.school_year.name} / {group.get_department_display()} / {group.grade} / {group.name}",
                "status": effective_status,
                "status_label": STATUSES[effective_status],
                "submission_id": str(adopted.id) if adopted else "",
                "pending_submission_count": sum(item.status == "pending" for item in submissions),
                "pending_supplement_count": pending_supplements,
                "student_count": counts["student_count"],
                "matched_photo_count": counts["matched_photo_count"],
                "missing_photo_count": counts["missing_photo_count"],
                "ambiguous_count": counts["ambiguous_count"],
                "duplicate_count": counts["duplicate_count"],
                "pending_exception_count": counts["ambiguous_count"] + pending_supplements,
                "photo_completion_rate": _rate(counts["matched_photo_count"], counts["student_count"]),
                "student_names": [student.name for student in students],
            }
        )
    rows.sort(key=lambda row: (row["school_year"], row["department"], row["grade"], row["class_name"]))
    totals = {
        "scope_count": len(rows),
        "adopted_scope_count": sum(row["status"] == "approved" for row in rows),
        "student_count": sum(row["student_count"] for row in rows),
        "matched_photo_count": sum(row["matched_photo_count"] for row in rows),
        "missing_photo_count": sum(row["missing_photo_count"] for row in rows),
        "pending_submission_count": sum(row["pending_submission_count"] for row in rows),
        "pending_supplement_count": sum(row["pending_supplement_count"] for row in rows),
        "pending_exception_count": sum(row["pending_exception_count"] for row in rows),
        "photo_completion_rate": _rate(sum(row["matched_photo_count"] for row in rows), sum(row["student_count"] for row in rows)),
    }
    return {
        "filters": values,
        "rows": rows,
        "totals": totals,
        "coverage": _coverage(totals["student_count"]),
        "status_choices": [(key, label) for key, label in STATUSES.items()],
    }


def _effective_submissions(values):
    for group in _scope_groups(values):
        submission = _effective_submission(group)
        if submission is not None:
            yield submission


def _anomaly_rows(values):
    rows = []
    seen = set()
    for submission in _effective_submissions(values):
        students = list(submission.students.order_by("id"))
        name_counts = Counter(student.name for student in students)
        photos = list(StudentPhoto.objects.filter(submission=submission).select_related("student"))
        photo_by_student = {photo.student_id: photo for photo in photos if photo.student_id is not None}

        def add(kind, student=None, photo_name="", reason=""):
            key = (kind, submission.id, student.id if student else "", photo_name, reason)
            if key in seen:
                return
            seen.add(key)
            rows.append(
                [
                    kind,
                    submission.school_year.name,
                    submission.get_department_display(),
                    submission.grade,
                    submission.class_group.name,
                    student.student_no if student else "",
                    student.name if student else "",
                    student.gender if student else "",
                    student.study_type if student else "",
                    photo_name,
                    reason,
                ]
            )

        for student in students:
            photo = photo_by_student.get(student.id)
            if student.is_duplicate or name_counts[student.name] > 1:
                add("同名学生", student, photo.original_name if photo else "", "当前名单或历史采用版本存在同名记录，请人工确认")
            if student.match_status == "ambiguous":
                add("待人工确认", student, photo.original_name if photo else "", "照片无法唯一匹配，禁止随机绑定")
            if student.match_status == "missing" or photo is None:
                add("缺少照片", student, photo.original_name if photo else "", "当前有效采用版本没有已匹配照片")

        for photo in photos:
            if photo.student_id is None:
                add("匹配失败", None, photo.original_name, "照片未绑定学生")

        supplements = PhotoSupplement.objects.filter(submission=submission, status="pending").prefetch_related("photos__student")
        for supplement in supplements:
            for photo in supplement.photos.all():
                add("待人工确认", photo.student, photo.original_name, "补传照片等待管理员确认")
    return rows


EXCEPTION_HEADERS = ["异常类型", "学年", "学部", "年级", "班级", "学生编号", "姓名", "性别", "就读类型", "照片文件名", "异常原因"]


def build_exception_workbook(filters=None):
    """Return an XLSX containing only anomalies from effective adopted versions."""
    values = _filters(filters)
    book = Workbook()
    sheet = book.active
    sheet.title = "异常名单"
    sheet.append(EXCEPTION_HEADERS)
    for row in _anomaly_rows(values):
        sheet.append([safe_excel_value(value) for value in row])
    sheet.freeze_panes = "A2"
    for index in range(1, sheet.max_column + 1):
        width = max(len(str(sheet.cell(row, index).value or "")) for row in range(1, sheet.max_row + 1))
        sheet.column_dimensions[get_column_letter(index)].width = min(max(width + 2, 12), 42)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def build_exception_rows(filters=None):
    """Expose normalized rows for a future admin download view and tests."""
    return _anomaly_rows(_filters(filters))
