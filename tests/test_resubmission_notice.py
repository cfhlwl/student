import hashlib

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from collector.models import AuditLog, ClassGroup, ResubmissionNotice, SchoolYear, Submission


def rejected_submission():
    year = SchoolYear.objects.create(name="2026")
    class_group = ClassGroup.objects.create(department="high", school_year=year, grade="高二", name="7")
    return Submission.objects.create(
        access_token_hash=hashlib.sha256(b"notice-submission").hexdigest(),
        department="high",
        school_year=year,
        grade="高二",
        class_group=class_group,
        status="rejected",
        source_filename="名单.xlsx",
    )


@pytest.mark.django_db
def test_rejected_submission_can_send_notice_and_public_home_shows_only_safe_class_message(client):
    submission = rejected_submission()
    admin = User.objects.create_user(username="notice-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)

    page = client.get(reverse("admin-submissions"))
    assert page.status_code == 200
    assert "通知重传" in page.content.decode()
    assert reverse("notify-resubmission", args=[submission.id]) in page.content.decode()

    response = client.post(
        reverse("notify-resubmission", args=[submission.id]),
        {"reason_code": "photo_unclear", "custom_reason": ""},
    )
    assert response.status_code == 302
    notice = ResubmissionNotice.objects.get(submission=submission)
    assert notice.reason == "照片不清晰"
    assert AuditLog.objects.filter(action="resubmission_notice_sent", target_id=str(submission.id)).exists()

    public = client.__class__().get(reverse("submit"))
    assert public.status_code == 200
    assert public.context["resubmission_notices"] == ["高二7班 照片不清晰，请重新上传照片！"]
    body = public.content.decode()
    assert "重传通知" in body
    assert "高二7班 照片不清晰，请重新上传照片！" in body
    assert "2026" not in public.context["resubmission_notices"][0]
    assert "高中部" not in public.context["resubmission_notices"][0]


@pytest.mark.django_db
def test_custom_notice_is_required_and_non_rejected_submission_cannot_be_notified(client):
    submission = rejected_submission()
    admin = User.objects.create_user(username="notice-admin-2", password="strong-password-123", is_staff=True)
    client.force_login(admin)

    invalid = client.post(
        reverse("notify-resubmission", args=[submission.id]),
        {"reason_code": "custom", "custom_reason": ""},
    )
    assert invalid.status_code == 302
    assert not ResubmissionNotice.objects.exists()

    custom = client.post(
        reverse("notify-resubmission", args=[submission.id]),
        {"reason_code": "custom", "custom_reason": "请补充正脸照片"},
    )
    assert custom.status_code == 302
    assert ResubmissionNotice.objects.get(submission=submission).reason == "请补充正脸照片"

    submission.status = "pending"
    submission.save(update_fields=["status"])
    blocked = client.post(
        reverse("notify-resubmission", args=[submission.id]),
        {"reason_code": "photo_unclear", "custom_reason": ""},
    )
    assert blocked.status_code == 302
    assert ResubmissionNotice.objects.get(submission=submission).reason == "请补充正脸照片"


@pytest.mark.django_db
def test_notice_styles_and_public_ticker_are_cache_versioned(client):
    submission = rejected_submission()
    notice = ResubmissionNotice.objects.create(
        submission=submission,
        reason_code="photo_unclear",
        reason="照片不清晰",
    )
    assert notice.is_active

    body = client.get(reverse("submit")).content.decode()
    assert "resubmission_notice.css?v=20261116-2" in body
    assert "submission_activity.js?v=20261116-2" in body
    assert "data-submission-activity" in body
    assert "public-notice-track" in body
    assert body.count("public-notice-group") == 2
