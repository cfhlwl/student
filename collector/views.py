import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.auth.decorators import user_passes_test
from django.db import IntegrityError
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .backup import BackupError, clear_collection_safely
from .maintenance import MaintenanceError, collection_write_gate, read_state, write_state
from .forms import ExportFilterForm, NamingRuleForm, SubmissionForm
from .models import AppSetting, AuditLog, ClassGroup, NamingRule, PhotoSupplement, SchoolYear, Student, StudentPhoto, Submission, SubmissionDocument, SupplementPhoto, UploadSession
from .preflight import run_preflight
from .services import ValidationError, create_photo_supplement, create_submission, export_students_to_tempfile, finalize_upload_session, grade_for, match_supplement_photo, normalize_grade, normalize_school_year, review_photo_supplement, review_submission_version, stage_upload_batch, start_upload_session, template_bytes, upload_session_status, validate_naming_rule
from .statistics import build_admin_statistics_context, build_exception_workbook


def client_ip(request):
    return request.META.get("REMOTE_ADDR")


class TemporaryZipResponse(FileResponse):
    def __init__(self, path, *args, **kwargs):
        self._temporary_path = Path(path)
        super().__init__(open(self._temporary_path, "rb"), *args, **kwargs)

    def close(self):
        try:
            super().close()
        finally:
            self._temporary_path.unlink(missing_ok=True)


def public_submit(request):
    if AuditLog.objects.filter(action="public_submit", ip_address=client_ip(request), created_at__gte=timezone.now() - timedelta(hours=1)).count() >= 20:
        return HttpResponse("提交过于频繁，请稍后再试。", status=429, content_type="text/plain; charset=utf-8")
    if request.method == "POST":
        form = SubmissionForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                submission, token = create_submission(
                    roster_upload=form.cleaned_data["roster"],
                    photo_uploads=request.FILES.getlist("photos"),
                    document_uploads=request.FILES.getlist("documents"),
                    note=form.cleaned_data.get("note", ""), ip_address=client_ip(request),
                )
            except ValidationError as exc:
                message = str(exc)
                if exc.errors:
                    message += "；" + "；".join(f"第 {item['row']} 行：{','.join(item['errors'])}" for item in exc.errors[:8])
                form.add_error(None, message)
            else:
                result_path = reverse("submit-result", args=[submission.id])
                result_url = f"{settings.SITE_URL}{result_path}" if settings.SITE_URL else request.build_absolute_uri(result_path)
                return render(request, "collector/submit_success.html", {"submission": submission, "token": token, "matched_count": submission.students.filter(match_status="matched").count(), "missing_count": submission.students.exclude(match_status="matched").count(), "result_url": result_url})
    else:
        form = SubmissionForm()
    return render(request, "collector/submit.html", {"form": form, "show_reselect_notice": request.GET.get("from") == "preflight"})


@require_GET
def submit_preflight_page(request):
    return render(request, "collector/submit_preflight.html", {"preflight_endpoint": reverse("submit-preflight")})


@require_POST
def submit_preflight(request):
    result = run_preflight(request.FILES.get("roster"), request.FILES.getlist("photos"))
    response = JsonResponse(result.as_dict())
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


@require_POST
def upload_start(request):
    try:
        expected_photo_count = int(request.POST.get("expected_photo_count", "0"))
        session, token, roster_count = start_upload_session(
            roster_upload=request.FILES.get("roster"),
            document_uploads=request.FILES.getlist("documents"),
            expected_photo_count=expected_photo_count,
            expected_document_count=int(request.POST.get("expected_document_count", str(len(request.FILES.getlist("documents"))))),
            photo_manifest=request.POST.get("photo_manifest", ""),
            document_manifest=request.POST.get("document_manifest", ""),
            roster_fingerprint=request.POST.get("roster_fingerprint", ""),
            note=request.POST.get("note", ""),
            idempotency_key=request.POST.get("idempotency_key", ""),
            ip_address=client_ip(request),
        )
    except (ValidationError, TypeError, ValueError, AttributeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({
        "upload_id": str(session.id),
        "upload_token": token,
        "roster_count": roster_count,
        "expected_photo_count": expected_photo_count,
        "photo_manifest_digest": session.photo_manifest_digest,
    })


@require_POST
def upload_photo_batch(request, session_id):
    try:
        sequence = int(request.POST.get("batch_no", "-1"))
        batch, count = stage_upload_batch(
            session_id,
            "photo",
            sequence,
            request.FILES.getlist("photos"),
            token=request.POST.get("upload_token"),
            manifest_digest=request.POST.get("manifest_digest", ""),
            roster_fingerprint=request.POST.get("roster_fingerprint"),
        )
    except UploadSession.DoesNotExist:
        return JsonResponse({"error": "上传会话不存在"}, status=404)
    except (ValidationError, TypeError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"batch_no": batch.sequence, "accepted": count})


@require_POST
def upload_document_batch(request, session_id):
    try:
        sequence = int(request.POST.get("batch_no", "-1"))
        batch, count = stage_upload_batch(
            session_id,
            "document",
            sequence,
            request.FILES.getlist("documents"),
            token=request.POST.get("upload_token", ""),
            manifest_digest=request.POST.get("manifest_digest", ""),
            roster_fingerprint=request.POST.get("roster_fingerprint"),
        )
    except UploadSession.DoesNotExist:
        return JsonResponse({"error": "上传会话不存在"}, status=404)
    except (ValidationError, TypeError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"batch_no": batch.sequence, "accepted": count})


@require_POST
def upload_complete(request, session_id):
    try:
        submission, token = finalize_upload_session(
            session_id=session_id,
            token=request.POST.get("upload_token", ""),
            manifest_digest=request.POST.get("manifest_digest", ""),
            roster_fingerprint=request.POST.get("roster_fingerprint"),
            ip_address=client_ip(request),
        )
    except UploadSession.DoesNotExist:
        return JsonResponse({"error": "上传会话不存在"}, status=404)
    except ValidationError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    result_path = reverse("submit-result", args=[submission.id])
    result_url = f"{settings.SITE_URL}{result_path}" if settings.SITE_URL else request.build_absolute_uri(result_path)
    return JsonResponse({"submission_id": str(submission.id), "token": token, "result_url": f"{result_url}?token={token}"})


@require_POST
def upload_status(request, session_id):
    try:
        data = upload_session_status(
            session_id=session_id,
            token=request.POST.get("upload_token", ""),
            manifest_digest=request.POST.get("manifest_digest"),
            roster_fingerprint=request.POST.get("roster_fingerprint"),
        )
    except UploadSession.DoesNotExist:
        response = JsonResponse({"error": "上传会话不存在或已过期"}, status=404)
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        return response
    except ValidationError as exc:
        response = JsonResponse({"error": str(exc)}, status=400)
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        return response
    if data.get("completed_submission_id") and data.get("result_token"):
        result_path = reverse("submit-result", args=[data["completed_submission_id"]])
        result_url = f"{settings.SITE_URL}{result_path}" if settings.SITE_URL else request.build_absolute_uri(result_path)
        data["result_url"] = f"{result_url}?token={data['result_token']}"
    response = JsonResponse(data)
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    return response


@require_GET
def download_template(request):
    response = HttpResponse(template_bytes(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="学生名单模板.xlsx"'
    return response


@require_GET
def submit_result(request, submission_id):
    token = request.GET.get("token", "")
    submission = get_object_or_404(Submission, id=submission_id)
    if not token or hashlib.sha256(token.encode()).hexdigest() != submission.access_token_hash:
        raise Http404
    supplements = submission.photo_supplements.prefetch_related("photos").all()
    return render(request, "collector/submit_result.html", {"submission": submission, "students": submission.students.all(), "supplements": supplements})


@require_POST
def submit_supplement(request, submission_id):
    token = request.POST.get("token", "")
    try:
        supplement, matched_count, ambiguous_count = create_photo_supplement(
            submission_id=submission_id,
            token=token,
            photo_uploads=request.FILES.getlist("photos"),
            note=request.POST.get("note", ""),
            ip_address=client_ip(request),
        )
    except Submission.DoesNotExist:
        raise Http404
    except ValidationError as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('submit-result', args=[submission_id])}?token={token}")
    messages.success(request, f"补传已提交：{matched_count} 张自动匹配，{ambiguous_count} 张待管理员人工确认。")
    return redirect(f"{reverse('submit-result', args=[submission_id])}?token={token}")


def is_staff(user):
    return user.is_authenticated and user.is_staff


staff_required = user_passes_test(is_staff, login_url="/admin/login/")


class StaffLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        if not form.get_user().is_staff:
            form.add_error(None, "该账号没有管理员权限")
            return self.form_invalid(form)
        return super().form_valid(form)


class StaffLogoutView(LogoutView):
    next_page = "/admin/login/"


@staff_required
def admin_dashboard(request):
    students = Student.objects.filter(submission__status="approved")
    class_stats = ClassGroup.objects.filter(is_active=True).annotate(
        student_count=Count("submissions__students", filter=Q(submissions__status="approved"), distinct=True),
        photo_count=Count("submissions__students__photo", filter=Q(submissions__status="approved", submissions__students__match_status="matched"), distinct=True),
    )
    context = {
        "student_count": students.count(), "photo_count": students.filter(match_status="matched").count(),
        "missing_count": students.exclude(match_status="matched").count(), "pending_count": Submission.objects.filter(status="pending").count(),
        "rejected_count": Submission.objects.filter(status="rejected").count(),
        "class_stats": class_stats, "upload_open": AppSetting.get_bool("public_upload_open", True),
    }
    return render(request, "collector/admin_dashboard.html", context)


@staff_required
def admin_statistics(request):
    context = build_admin_statistics_context(request.GET)
    query = request.GET.urlencode()
    context["exception_export_url"] = reverse("admin-statistics-exceptions") + (("?" + query) if query else "")
    return render(request, "collector/admin_statistics.html", context)


@require_GET
@staff_required
def admin_statistics_exceptions(request):
    response = HttpResponse(
        build_exception_workbook(request.GET),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = "attachment; filename=collection-exceptions.xlsx"
    response["Cache-Control"] = "no-store"
    return response


@staff_required
def admin_submissions(request):
    submissions = Submission.objects.select_related("class_group", "school_year", "reviewed_by").all()
    if request.GET.get("status"): submissions = submissions.filter(status=request.GET["status"])
    supplements = PhotoSupplement.objects.filter(status="pending").select_related("submission", "submission__class_group").prefetch_related("photos", "photos__student")
    return render(request, "collector/admin_submissions.html", {"submissions": submissions, "supplements": supplements})


@require_POST
@staff_required
def review_supplement(request, supplement_id):
    try:
        result = review_photo_supplement(supplement_id=supplement_id, action=request.POST.get("action"), actor=request.user, ip_address=client_ip(request))
    except PhotoSupplement.DoesNotExist:
        raise Http404
    except ValidationError as exc:
        messages.error(request, str(exc))
    else:
        if result.status == "expired":
            messages.warning(request, "补传未采用：原提交版本已被新版本替代，照片保留供审计。")
        else:
            messages.success(request, "补传照片已" + ("采用" if request.POST.get("action") == "approved" else "退回") + "。")
    return redirect("admin-submissions")


@require_POST
@staff_required
def manual_supplement_match(request, photo_id):
    try:
        photo = match_supplement_photo(photo_id=photo_id, student_id=request.POST.get("student_id") or None, actor=request.user, ip_address=client_ip(request))
    except SupplementPhoto.DoesNotExist:
        raise Http404
    except (ValidationError, IntegrityError) as exc:
        messages.error(request, str(exc) or "绑定操作未完成，请刷新后重试。")
    else:
        if photo.student_id:
            messages.success(request, f"已将 {photo.original_name} 绑定到 {photo.student.name}。")
        else:
            messages.success(request, f"已解除 {photo.original_name} 的人工绑定。")
    return redirect("admin-submissions")


@require_POST
@staff_required
def review_submission(request, submission_id):
    action = request.POST.get("action")
    if action in {"approved", "rejected"}:
        try:
            review_submission_version(submission_id=submission_id, action=action, actor=request.user, ip_address=client_ip(request))
        except Submission.DoesNotExist:
            raise Http404
        messages.success(request, "提交版本已" + ("采用" if action == "approved" else "退回") + "。")
    return redirect("admin-submissions")


def filter_values(form):
    return {f"{key if key != 'school_year' else 'school_year__name'}": value for key, value in form.cleaned_data.items() if value and key != "has_photo"}


@staff_required
def admin_students(request):
    form = ExportFilterForm(request.GET or None)
    qs = Student.objects.select_related("school_year", "submission").filter(submission__status="approved")
    filters = {}
    if form.is_valid():
        filters = filter_values(form)
        qs = qs.filter(**filters)
        if form.cleaned_data.get("has_photo") == "yes": qs = qs.filter(match_status="matched")
        if form.cleaned_data.get("has_photo") == "no": qs = qs.exclude(match_status="matched")
    return render(request, "collector/admin_students.html", {"students": qs, "form": form, "filters_json": json.dumps(filters, ensure_ascii=False), "unmatched_photos": StudentPhoto.objects.filter(student__isnull=True, submission__status__in=["pending", "approved"]).select_related("submission")})


@require_POST
@staff_required
def manual_match(request, photo_id):
    photo = get_object_or_404(StudentPhoto, id=photo_id, student__isnull=True, submission__status__in=["pending", "approved"])
    student = get_object_or_404(Student, id=request.POST.get("student_id"), submission=photo.submission)
    if StudentPhoto.objects.filter(student=student).exists():
        messages.error(request, "该学生已有照片，不能覆盖原照片。")
    else:
        try:
            with collection_write_gate():
                photo.student = student; photo.save(update_fields=["student"])
                student.match_status = "matched"; student.save(update_fields=["match_status"])
                AuditLog.objects.create(actor=request.user, action="manual_photo_match", target_type="Student", target_id=str(student.id), detail={"photo_id": photo.id}, ip_address=client_ip(request))
        except MaintenanceError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"已将 {photo.original_name} 绑定到 {student.name}。")
    return redirect("admin-students")


@require_GET
@staff_required
def protected_photo(request, photo_id):
    photo = get_object_or_404(StudentPhoto, id=photo_id)
    return FileResponse(photo.file.open("rb"), content_type=photo.content_type)


@require_GET
@staff_required
def protected_document(request, document_id):
    document = get_object_or_404(SubmissionDocument, id=document_id)
    response = FileResponse(document.file.open("rb"), content_type=document.content_type)
    response["Content-Disposition"] = f'attachment; filename="{document.original_name}"'
    return response


@require_GET
@staff_required
def export_zip(request):
    # An empty query string still means "export all adopted data". Passing
    # None creates an unbound form, whose is_valid() is always False.
    form = ExportFilterForm(request.GET)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    filters = filter_values(form)
    if form.cleaned_data.get("has_photo"): filters["has_photo"] = form.cleaned_data["has_photo"]
    temp_path, count, preview = export_students_to_tempfile(filters, actor=request.user, ip_address=client_ip(request))
    response = TemporaryZipResponse(temp_path, content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="学生资料导出.zip"'
    response["X-Export-Student-Count"] = str(count)
    response["X-Export-Preview"] = json.dumps(preview, ensure_ascii=False)
    return response


@staff_required
def admin_naming(request):
    rule = NamingRule.current()
    if request.method == "POST":
        form = NamingRuleForm(request.POST)
        if form.is_valid():
            rule.mode = form.cleaned_data["mode"]
            rule.template = form.cleaned_data["template"]
            rule.male_code = form.cleaned_data["male_code"]
            rule.female_code = form.cleaned_data["female_code"]
            rule.separator = form.cleaned_data["separator"]
            try:
                validate_naming_rule(rule)
            except ValidationError as exc:
                form.add_error("template", str(exc))
            else:
                rule.updated_by = request.user
                rule.save()
                messages.success(request, "照片命名规则已保存。")
                return redirect("admin-naming")
    else:
        form = NamingRuleForm(initial={"mode": rule.mode, "template": rule.template, "male_code": rule.male_code, "female_code": rule.female_code, "separator": rule.separator})
    return render(request, "collector/admin_naming.html", {"form": form, "rule": rule})


@require_POST
@staff_required
def toggle_upload(request):
    current = AppSetting.get_bool("public_upload_open", True)
    opening = not current
    if opening:
        # Re-opening after a successful clear is an explicit administrator
        # action; ordinary public-upload toggles do not alter this marker.
        write_state("open")
    AppSetting.set_value("public_upload_open", opening)
    AuditLog.objects.create(actor=request.user, action="toggle_public_upload", detail={"open": not current}, ip_address=client_ip(request))
    messages.success(request, "公开上传已" + ("开启" if not current else "关闭") + "。")
    return redirect("admin-dashboard")


@require_POST
@staff_required
def clear_test_data(request):
    if request.POST.get("confirm_clear") != "YES":
        messages.error(request, "清空操作未确认，未删除任何资料。")
        return redirect("admin-dashboard")
    admin_password = request.POST.get("admin_password", "")
    if not admin_password or not request.user.check_password(admin_password):
        messages.error(request, "管理员密码校验失败，未删除任何资料。")
        return redirect("admin-dashboard")
    backup_dir = Path(getattr(settings, "COLLECTION_BACKUP_DIR", settings.BASE_DIR / "data" / "backups"))
    backup_path = backup_dir / f"before-clear-{timezone.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:10]}.zip"
    try:
        counts = clear_collection_safely(backup_path)
    except (BackupError, OSError) as exc:
        messages.error(request, f"清空未完成，资料未删除；备份或一致性检查失败：{exc}")
        return redirect("admin-dashboard")
    messages.success(request, f"已先备份并初始化数据库：清除提交 {counts['submissions']} 个、学生 {counts['students']} 条、照片 {counts['photos']} 张、文档 {counts['documents']} 个、学生名单 {counts['rosters']} 个；管理员账号已保留。采集入口已关闭，请明确点击“开启公开上传”后再开始下一轮采集。备份：{backup_path.name}")
    return redirect("admin-dashboard")


@staff_required
def admin_classes(request):
    if request.method == "POST":
        if request.POST.get("action") == "set_scope":
            scope = request.POST.get("scope", "all")
            AppSetting.set_value("collection_scope", "configured" if scope == "configured" else "all")
            AuditLog.objects.create(actor=request.user, action="set_collection_scope", detail={"scope": AppSetting.get_value("collection_scope")}, ip_address=client_ip(request))
            messages.success(request, "采集范围已切换为" + ("仅已配置班级" if scope == "configured" else "全校全部范围") + "。")
            return redirect("admin-classes")
        year_name = request.POST.get("school_year", "").strip()
        department = request.POST.get("department", "")
        grade_name = request.POST.get("grade", "").strip()
        class_name = request.POST.get("name", "").strip()
        if year_name and department and class_name:
            try:
                year_name = normalize_school_year(year_name)
                grade = normalize_grade(grade_name, "初中部" if department == "middle" else "高中部") if grade_name else grade_for(department, year_name)
            except ValidationError as exc:
                messages.error(request, str(exc))
            else:
                year, _ = SchoolYear.objects.get_or_create(name=year_name, defaults={"is_active": True})
                ClassGroup.objects.get_or_create(department=department, school_year=year, grade=grade, name=class_name, defaults={"is_active": True})
                messages.success(request, "班级范围已保存，年级为" + grade + "。")
            return redirect("admin-classes")
    return render(request, "collector/admin_classes.html", {"classes": ClassGroup.objects.select_related("school_year"), "years": SchoolYear.objects.all(), "collection_scope": AppSetting.get_value("collection_scope", "all")})

