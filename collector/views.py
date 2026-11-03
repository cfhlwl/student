import hashlib
import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import ExportFilterForm, NamingRuleForm, SubmissionForm
from .models import AppSetting, AuditLog, ClassGroup, NamingRule, SchoolYear, Student, StudentPhoto, Submission, SubmissionDocument
from .services import ValidationError, create_submission, export_students, template_bytes, validate_naming_rule


def client_ip(request):
    return request.META.get("REMOTE_ADDR")


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
                return render(request, "collector/submit_success.html", {"submission": submission, "token": token, "matched_count": submission.students.filter(match_status="matched").count(), "missing_count": submission.students.exclude(match_status="matched").count(), "result_url": request.build_absolute_uri(reverse("submit-result", args=[submission.id]))})
    else:
        form = SubmissionForm()
    return render(request, "collector/submit.html", {"form": form})


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
    return render(request, "collector/submit_result.html", {"submission": submission, "students": submission.students.all()})


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
        "class_stats": class_stats, "upload_open": AppSetting.get_bool("public_upload_open", True),
    }
    return render(request, "collector/admin_dashboard.html", context)


@staff_required
def admin_submissions(request):
    submissions = Submission.objects.select_related("class_group", "school_year", "reviewed_by").all()
    if request.GET.get("status"): submissions = submissions.filter(status=request.GET["status"])
    return render(request, "collector/admin_submissions.html", {"submissions": submissions})


@require_POST
@staff_required
def review_submission(request, submission_id):
    submission = get_object_or_404(Submission, id=submission_id)
    action = request.POST.get("action")
    if action in {"approved", "rejected"}:
        submission.status = action
        submission.reviewed_at = timezone.now()
        submission.reviewed_by = request.user
        submission.save(update_fields=["status", "reviewed_at", "reviewed_by"])
        AuditLog.objects.create(actor=request.user, action=f"submission_{action}", target_type="Submission", target_id=str(submission.id), ip_address=client_ip(request))
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
    return render(request, "collector/admin_students.html", {"students": qs, "form": form, "filters_json": json.dumps(filters, ensure_ascii=False), "unmatched_photos": StudentPhoto.objects.filter(student__isnull=True).select_related("submission")})


@require_POST
@staff_required
def manual_match(request, photo_id):
    photo = get_object_or_404(StudentPhoto, id=photo_id, student__isnull=True)
    student = get_object_or_404(Student, id=request.POST.get("student_id"), submission=photo.submission)
    if StudentPhoto.objects.filter(student=student).exists():
        messages.error(request, "该学生已有照片，不能覆盖原照片。")
    else:
        photo.student = student; photo.save(update_fields=["student"])
        student.match_status = "matched"; student.save(update_fields=["match_status"])
        AuditLog.objects.create(actor=request.user, action="manual_photo_match", target_type="Student", target_id=str(student.id), detail={"photo_id": photo.id}, ip_address=client_ip(request))
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
    form = ExportFilterForm(request.GET or None)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    filters = filter_values(form)
    if form.cleaned_data.get("has_photo"): filters["has_photo"] = form.cleaned_data["has_photo"]
    payload, count, preview = export_students(filters, actor=request.user, ip_address=client_ip(request))
    response = HttpResponse(payload, content_type="application/zip")
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
    AppSetting.set_value("public_upload_open", not current)
    AuditLog.objects.create(actor=request.user, action="toggle_public_upload", detail={"open": not current}, ip_address=client_ip(request))
    messages.success(request, "公开上传已" + ("开启" if not current else "关闭") + "。")
    return redirect("admin-dashboard")


@staff_required
def admin_classes(request):
    if request.method == "POST":
        year_name = request.POST.get("school_year", "").strip()
        if year_name and request.POST.get("grade") and request.POST.get("name"):
            year, _ = SchoolYear.objects.get_or_create(name=year_name, defaults={"is_active": True})
            ClassGroup.objects.get_or_create(department=request.POST.get("department"), school_year=year, grade=request.POST.get("grade"), name=request.POST.get("name"), defaults={"is_active": True})
            messages.success(request, "班级范围已保存。")
            return redirect("admin-classes")
    return render(request, "collector/admin_classes.html", {"classes": ClassGroup.objects.select_related("school_year"), "years": SchoolYear.objects.all()})

