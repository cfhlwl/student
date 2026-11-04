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


def supplement_photo_path(instance, filename):
    return Path("supplements", str(instance.supplement_id), "photos", filename).as_posix()


class PhotoSupplement(models.Model):
    STATUS_CHOICES = [("pending", "待管理员确认"), ("approved", "已采用"), ("rejected", "已退回"), ("expired", "原提交已失效")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey("Submission", on_delete=models.CASCADE, related_name="photo_supplements")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    note = models.CharField(max_length=500, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_photo_supplements")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["submission"],
                condition=Q(status="pending"),
                name="unique_pending_photo_supplement_per_submission",
            ),
        ]


class SupplementPhoto(models.Model):
    MATCH_STATUSES = [("matched", "已匹配"), ("ambiguous", "待人工确认")]
    supplement = models.ForeignKey(PhotoSupplement, on_delete=models.CASCADE, related_name="photos")
    student = models.ForeignKey(Student, null=True, blank=True, on_delete=models.CASCADE, related_name="supplement_photos")
    original_name = models.CharField(max_length=255)
    file = models.FileField(upload_to=supplement_photo_path)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    sha256 = models.CharField(max_length=64)
    match_status = models.CharField(max_length=12, choices=MATCH_STATUSES, default="ambiguous")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["supplement", "student"],
                condition=Q(student__isnull=False),
                name="unique_supplement_student_binding",
            ),
        ]


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
    start_idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
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
    document_manifest = models.JSONField(default=list, blank=True)
    document_manifest_digest = models.CharField(max_length=64, blank=True)
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
        # Read paths must not create a row. The admin write path creates and
        # saves an explicit default object when the naming rule is first set.
        return obj or cls()


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


# V2 generic collection foundation.  These models intentionally live beside
# the legacy models so the first migration can add tables without changing or
# renaming any V1 table.
class CollectionProject(models.Model):
    STATUS_CHOICES = [
        ("active", "启用"),
        ("disabled", "停用"),
        ("archived", "归档"),
    ]
    slug = models.SlugField(max_length=80, unique=True, allow_unicode=False)
    name = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="active")
    active_config_version = models.ForeignKey(
        "ProjectConfigVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="active_for_projects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]


class TemplateDefinition(models.Model):
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="templates")
    key = models.CharField(max_length=80)
    name = models.CharField(max_length=160)
    preset = models.CharField(max_length=80, blank=True)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "key"], name="unique_v2_template_key")]


class TemplateVersion(models.Model):
    STATES = [("draft", "草稿"), ("published", "已发布"), ("retired", "已停用")]
    template = models.ForeignKey(TemplateDefinition, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField()
    state = models.CharField(max_length=12, choices=STATES, default="draft")
    schema_json = models.JSONField(default=dict)
    mapping_json = models.JSONField(default=dict)
    matching_json = models.JSONField(default=dict)
    field_export_json = models.JSONField(default=dict)
    schema_sha256 = models.CharField(max_length=64)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["template", "version"], name="unique_v2_template_version")]


class ProjectConfigVersion(models.Model):
    STATES = [("draft", "草稿"), ("published", "已发布"), ("retired", "已停用")]
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="config_versions")
    version = models.PositiveIntegerField()
    state = models.CharField(max_length=12, choices=STATES, default="draft")
    settings_json = models.JSONField(default=dict)
    export_policy_json = models.JSONField(default=dict)
    settings_sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_v2_config_versions")
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "version"], name="unique_v2_config_version")]


class CollectionEntry(models.Model):
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="entries")
    slug = models.SlugField(max_length=80, allow_unicode=False)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    scope_json = models.JSONField(default=dict)
    published_template_version = models.ForeignKey(
        TemplateVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_entries",
    )
    is_default = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "slug"], name="unique_v2_entry_slug"),
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(is_default=True),
                name="unique_v2_default_entry_per_project",
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.published_template_version_id and self.published_template_version.template.project_id != self.project_id:
            raise ValidationError({"published_template_version": "模板版本必须属于同一项目"})
        if self.published_template_version_id and self.published_template_version.state != "published":
            raise ValidationError({"published_template_version": "入口只能指向已发布模板版本"})


class CollectionActivity(models.Model):
    ADOPTION_MODES = [
        ("replace_scope", "按范围替换"),
        ("record_revision", "单记录修订"),
        ("global_claim", "全局唯一"),
        ("append", "追加报名"),
    ]
    STATUSES = [
        ("draft", "草稿"),
        ("open", "开放"),
        ("paused", "暂停"),
        ("closed", "关闭"),
        ("archived", "归档"),
    ]
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="activities")
    entry = models.ForeignKey(CollectionEntry, on_delete=models.PROTECT, related_name="activities")
    name = models.CharField(max_length=160)
    scope_json = models.JSONField(default=dict)
    adoption_mode = models.CharField(max_length=20, choices=ADOPTION_MODES, default="replace_scope")
    status = models.CharField(max_length=12, choices=STATUSES, default="draft")
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["entry", "name"], name="unique_v2_activity_name_per_entry")]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.entry_id and self.entry.project_id != self.project_id:
            raise ValidationError({"entry": "采集入口必须属于同一项目"})


class CollectionRun(models.Model):
    STATUSES = [
        ("uploading", "上传中"),
        ("finalizing", "完成中"),
        ("submitted", "已提交"),
        ("reviewing", "审核中"),
        ("adopted", "已采用"),
        ("superseded", "已替代"),
        ("rejected", "已退回"),
        ("abandoned", "已放弃"),
        ("expired", "已过期"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    activity = models.ForeignKey(CollectionActivity, on_delete=models.PROTECT, related_name="runs")
    entry = models.ForeignKey(CollectionEntry, on_delete=models.PROTECT, related_name="runs")
    template_version = models.ForeignKey(TemplateVersion, on_delete=models.PROTECT, related_name="runs")
    config_version = models.ForeignKey(ProjectConfigVersion, on_delete=models.PROTECT, related_name="runs")
    scope_json = models.JSONField(default=dict)
    status = models.CharField(max_length=14, choices=STATUSES, default="uploading")
    access_token_hash = models.CharField(max_length=64, unique=True)
    template_schema_sha256 = models.CharField(max_length=64)
    config_sha256 = models.CharField(max_length=64)
    export_snapshot_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.activity_id and self.activity.entry_id != self.entry_id:
            raise ValidationError({"entry": "提交入口必须与采集活动入口一致"})
        if self.activity_id and self.activity.project_id != self.entry.project_id:
            raise ValidationError({"entry": "提交入口与采集活动必须属于同一项目"})
        if self.template_version_id and self.template_version.template.project_id != self.entry.project_id:
            raise ValidationError({"template_version": "模板版本必须属于提交入口项目"})
        if self.template_version_id and self.template_version.state != "published":
            raise ValidationError({"template_version": "提交只能绑定已发布模板版本"})
        if self.config_version_id and self.config_version.project_id != self.entry.project_id:
            raise ValidationError({"config_version": "配置版本必须属于提交入口项目"})
        if self.config_version_id and self.config_version.state != "published":
            raise ValidationError({"config_version": "提交只能绑定已发布配置版本"})


class RecordIdentity(models.Model):
    ENTITY_TYPES = [("student", "学生"), ("employee", "员工"), ("asset", "资产"), ("signup", "报名")]
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="identities")
    entity_type = models.CharField(max_length=20, choices=ENTITY_TYPES)
    stable_key = models.CharField(max_length=255)
    current_record = models.ForeignKey("CollectionRecord", null=True, blank=True, on_delete=models.SET_NULL, related_name="current_for_identities")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "entity_type", "stable_key"], name="unique_v2_record_identity")]


class CollectionRecord(models.Model):
    STATUSES = [
        ("staged", "暂存"),
        ("validated", "已校验"),
        ("pending", "待审核"),
        ("effective", "已生效"),
        ("rejected", "已退回"),
        ("superseded", "已替代"),
    ]
    run = models.ForeignKey(CollectionRun, on_delete=models.PROTECT, related_name="records")
    identity = models.ForeignKey(RecordIdentity, on_delete=models.PROTECT, related_name="records")
    external_key = models.CharField(max_length=255, blank=True)
    values_json = models.JSONField(default=dict)
    normalized_json = models.JSONField(default=dict)
    status = models.CharField(max_length=14, choices=STATUSES, default="staged")
    revision_no = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "identity"], name="unique_v2_record_per_run_identity"),
            models.UniqueConstraint(
                fields=["run", "external_key"],
                condition=~Q(external_key=""),
                name="unique_v2_external_key_per_run",
            ),
            models.UniqueConstraint(fields=["identity", "revision_no"], name="unique_v2_identity_revision"),
        ]


class EffectiveVersion(models.Model):
    activity = models.ForeignKey(CollectionActivity, on_delete=models.PROTECT, related_name="effective_versions")
    scope_key = models.CharField(max_length=255, default="*")
    identity = models.ForeignKey(RecordIdentity, null=True, blank=True, on_delete=models.PROTECT, related_name="effective_versions")
    run = models.ForeignKey(CollectionRun, on_delete=models.PROTECT, related_name="effective_versions")
    record = models.ForeignKey(CollectionRecord, on_delete=models.PROTECT, related_name="effective_versions")
    is_current = models.BooleanField(default=True)
    adopted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["activity", "scope_key"],
                condition=Q(is_current=True),
                name="unique_v2_current_scope_version",
            ),
            models.UniqueConstraint(
                fields=["activity", "identity"],
                condition=Q(is_current=True, identity__isnull=False),
                name="unique_v2_current_identity_version",
            ),
        ]


class RecordUniqueValue(models.Model):
    NAMESPACE_KINDS = [("project", "项目全局"), ("scope", "项目范围"), ("template", "模板版本")]
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="unique_values")
    field_key = models.CharField(max_length=120)
    namespace_kind = models.CharField(max_length=12, choices=NAMESPACE_KINDS)
    namespace_key = models.CharField(max_length=255)
    canonical_value = models.CharField(max_length=500)
    owner_identity = models.ForeignKey(RecordIdentity, on_delete=models.PROTECT, related_name="unique_value_claims")
    source_record = models.ForeignKey(CollectionRecord, null=True, blank=True, on_delete=models.PROTECT, related_name="unique_value_claims")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "field_key", "namespace_kind", "namespace_key", "canonical_value"],
                condition=Q(is_active=True),
                name="unique_v2_active_unique_claim",
            ),
        ]


def v2_asset_path(instance, filename):
    return Path("v2", "projects", str(instance.project_id), "runs", str(instance.run_id), "assets", instance.storage_name).as_posix()


class CollectionAsset(models.Model):
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="assets")
    run = models.ForeignKey(CollectionRun, on_delete=models.PROTECT, related_name="assets")
    record = models.ForeignKey(CollectionRecord, null=True, blank=True, on_delete=models.PROTECT, related_name="assets")
    kind = models.CharField(max_length=30)
    original_name = models.CharField(max_length=255)
    storage_name = models.CharField(max_length=255)
    file = models.FileField(upload_to=v2_asset_path)
    mime = models.CharField(max_length=120)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "storage_name"], name="unique_v2_asset_storage_name_per_run")]

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.run_id and self.run.entry.project_id != self.project_id:
            raise ValidationError({"project": "资产项目必须与提交入口项目一致"})
        if self.record_id and self.record.run_id != self.run_id:
            raise ValidationError({"record": "资产记录必须属于同一提交"})


class ReviewAction(models.Model):
    run = models.ForeignKey(CollectionRun, null=True, blank=True, on_delete=models.PROTECT, related_name="review_actions")
    record = models.ForeignKey(CollectionRecord, null=True, blank=True, on_delete=models.PROTECT, related_name="review_actions")
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="v2_review_actions")
    action = models.CharField(max_length=40)
    before_json = models.JSONField(default=dict)
    after_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class ProjectMember(models.Model):
    ROLES = [("owner", "所有者"), ("editor", "编辑者"), ("reviewer", "审核者"), ("exporter", "导出者"), ("viewer", "查看者")]
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="members")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="collection_projects")
    role = models.CharField(max_length=12, choices=ROLES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "user"], name="unique_v2_project_member")]


class ImportReservation(models.Model):
    project = models.ForeignKey(CollectionProject, on_delete=models.PROTECT, related_name="import_reservations")
    reservation_key = models.CharField(max_length=160)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "reservation_key"], name="unique_v2_import_reservation")]

