import secrets
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class SchoolYear(models.Model):
    name = models.CharField(max_length=20, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-name"]

    def __str__(self):
        return self.name


class ClassGroup(models.Model):
    DEPARTMENTS = [("middle", "初中部"), ("high", "高中部")]
    GRADES = [("初一", "初一"), ("初二", "初二"), ("初三", "初三"), ("高一", "高一"), ("高二", "高二"), ("高三", "高三")]
    department = models.CharField(max_length=12, choices=DEPARTMENTS)
    school_year = models.ForeignKey(SchoolYear, on_delete=models.PROTECT, related_name="classes")
    grade = models.CharField(max_length=10, choices=GRADES)
    name = models.CharField(max_length=30)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["department", "grade", "name"]
        constraints = [models.UniqueConstraint(fields=["department", "school_year", "grade", "name"], name="unique_class_scope")]

    def __str__(self):
        return f"{self.get_department_display()} / {self.school_year} / {self.grade}{self.name}"


def roster_path(instance, filename):
    return Path("submissions", str(instance.id), "roster", filename).as_posix()


class Submission(models.Model):
    STATUS_CHOICES = [("pending", "待审核"), ("approved", "已采用"), ("rejected", "已退回")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    access_token_hash = models.CharField(max_length=64, unique=True)
    department = models.CharField(max_length=12, choices=ClassGroup.DEPARTMENTS)
    school_year = models.ForeignKey(SchoolYear, on_delete=models.PROTECT, related_name="submissions")
    grade = models.CharField(max_length=10, choices=ClassGroup.GRADES)
    class_group = models.ForeignKey(ClassGroup, on_delete=models.PROTECT, related_name="submissions")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    source_filename = models.CharField(max_length=255)
    note = models.CharField(max_length=500, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_submissions")
    roster_file = models.FileField(upload_to=roster_path, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["class_group"],
                condition=Q(status="approved"),
                name="unique_approved_submission_per_class",
            ),
        ]

    @staticmethod
    def make_token():
        return secrets.token_urlsafe(32)

    def __str__(self):
        return f"{self.class_group} / {self.id}"


class Student(models.Model):
    GENDERS = [("男", "男"), ("女", "女")]
    STUDY_TYPES = [("走读", "走读"), ("半走读", "半走读"), ("住宿", "住宿")]
    MATCH_STATUSES = [("matched", "已匹配"), ("missing", "缺少照片"), ("ambiguous", "匹配失败/需人工确认")]
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="students")
    student_no = models.CharField(max_length=80, blank=True)
    name = models.CharField(max_length=80)
    gender = models.CharField(max_length=2, choices=GENDERS, blank=True)
    department = models.CharField(max_length=12, choices=ClassGroup.DEPARTMENTS)
    school_year = models.ForeignKey(SchoolYear, on_delete=models.PROTECT, related_name="students")
    grade = models.CharField(max_length=10, choices=ClassGroup.GRADES)
    class_name = models.CharField(max_length=30)
    study_type = models.CharField(max_length=10, choices=STUDY_TYPES)
    photo_filename_hint = models.CharField(max_length=255, blank=True)
    remark = models.CharField(max_length=500, blank=True)
    match_status = models.CharField(max_length=12, choices=MATCH_STATUSES, default="missing")
    is_duplicate = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["class_name", "student_no", "name"]


def upload_path(instance, filename):
    return Path("submissions", str(instance.submission_id), "photos", filename).as_posix()


def document_path(instance, filename):
    return Path("submissions", str(instance.submission_id), "documents", filename).as_posix()


class StudentPhoto(models.Model):
    student = models.OneToOneField(Student, on_delete=models.CASCADE, related_name="photo", null=True, blank=True)
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="photos")
    original_name = models.CharField(max_length=255)
    file = models.FileField(upload_to=upload_path)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    sha256 = models.CharField(max_length=64)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class SubmissionDocument(models.Model):
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="documents")
    original_name = models.CharField(max_length=255)
    file = models.FileField(upload_to=document_path)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    uploaded_at = models.DateTimeField(auto_now_add=True)


def staged_roster_path(instance, filename):
    return Path("upload_sessions", str(instance.id), "roster", filename).as_posix()


class UploadSession(models.Model):
    STATUS_CHOICES = [("pending", "上传中"), ("finalizing", "正在完成"), ("completed", "已完成"), ("failed", "失败")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    access_token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    roster_file = models.FileField(upload_to=staged_roster_path)
    roster_original_name = models.CharField(max_length=255)
    roster_sha256 = models.CharField(max_length=64, blank=True)
    roster_fingerprint = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=500, blank=True)
    expected_student_count = models.PositiveIntegerField(default=0)
    expected_photo_count = models.PositiveIntegerField(default=0)
    expected_document_count = models.PositiveIntegerField(default=0)
    photo_manifest = models.JSONField(default=list, blank=True)
    photo_manifest_digest = models.CharField(max_length=64, blank=True)
    completed_submission = models.OneToOneField("Submission", null=True, blank=True, on_delete=models.SET_NULL, related_name="upload_session")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @staticmethod
    def make_token():
        return secrets.token_urlsafe(32)


def staged_upload_path(instance, filename):
    return Path(
        "upload_sessions",
        str(instance.batch.session_id),
        instance.batch.kind,
        str(instance.batch.sequence),
        filename,
    ).as_posix()


class UploadBatch(models.Model):
    KIND_CHOICES = [("photo", "照片"), ("document", "文档")]
    session = models.ForeignKey(UploadSession, on_delete=models.CASCADE, related_name="batches")
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    sequence = models.PositiveIntegerField()
    digest = models.CharField(max_length=64)
    count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "kind", "sequence"], name="unique_upload_batch")]


class StagedUpload(models.Model):
    batch = models.ForeignKey(UploadBatch, on_delete=models.CASCADE, related_name="uploads")
    ordinal = models.PositiveIntegerField()
    original_name = models.CharField(max_length=255)
    file = models.FileField(upload_to=staged_upload_path)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["batch__kind", "batch__sequence", "ordinal", "id"]
        constraints = [models.UniqueConstraint(fields=["batch", "ordinal"], name="unique_staged_upload_ordinal")]


class NamingRule(models.Model):
    MODES = [("name", "仅姓名"), ("gender_no", "姓名 + 性别编号"), ("custom", "自定义字段组合")]
    mode = models.CharField(max_length=20, choices=MODES, default="name")
    template = models.CharField(max_length=200, default="{姓名}")
    male_code = models.CharField(max_length=10, default="01")
    female_code = models.CharField(max_length=10, default="02")
    separator = models.CharField(max_length=5, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)

    @classmethod
    def current(cls):
        obj = cls.objects.first()
        return obj or cls.objects.create()


class AppSetting(models.Model):
    key = models.CharField(max_length=80, unique=True)
    value = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_value(cls, key, default=""):
        obj = cls.objects.filter(key=key).first()
        return default if obj is None else obj.value

    @classmethod
    def get_bool(cls, key, default=False):
        obj = cls.objects.filter(key=key).first()
        return default if obj is None else obj.value.lower() in {"1", "true", "yes", "on"}

    @classmethod
    def set_value(cls, key, value):
        cls.objects.update_or_create(key=key, defaults={"value": str(value)})


class AuditLog(models.Model):
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

