from datetime import timedelta
import hashlib

import pytest
from django.urls import reverse
from django.utils import timezone

from collector.models import ClassGroup, SchoolYear, Submission


def make_submission(class_group, status, token):
    return Submission.objects.create(
        access_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        department=class_group.department,
        school_year=class_group.school_year,
        grade=class_group.grade,
        class_group=class_group,
        status=status,
        source_filename=f"{token}.xlsx",
    )


@pytest.mark.django_db
def test_public_home_shows_latest_de_duplicated_submission_activity(client):
    school_year = SchoolYear.objects.create(name="2026")
    pending_class = ClassGroup.objects.create(department="high", school_year=school_year, grade="高三", name="1")
    approved_class = ClassGroup.objects.create(department="high", school_year=school_year, grade="高二", name="2")
    rejected_class = ClassGroup.objects.create(department="middle", school_year=school_year, grade="初三", name="3")
    other_year = SchoolYear.objects.create(name="2025")
    same_visible_class = ClassGroup.objects.create(department="high", school_year=other_year, grade="高三", name="1")

    old_pending = make_submission(pending_class, "pending", "pending-old")
    latest_pending = make_submission(pending_class, "pending", "pending-latest")
    approved = make_submission(approved_class, "approved", "approved")
    make_submission(rejected_class, "rejected", "rejected")
    duplicate_visible_submission = make_submission(same_visible_class, "pending", "pending-other-year")
    now = timezone.now()
    Submission.objects.filter(id=old_pending.id).update(submitted_at=now - timedelta(days=1))
    Submission.objects.filter(id=latest_pending.id).update(submitted_at=now)
    Submission.objects.filter(id=approved.id).update(submitted_at=now - timedelta(hours=1))
    Submission.objects.filter(id=duplicate_visible_submission.id).update(submitted_at=now - timedelta(hours=2))

    response = client.get(reverse("submit"))

    assert response.status_code == 200
    assert response.context["submission_activity_uploaded"] == [
        "高三1班上传了资料",
        "高二2班上传了资料",
    ]
    assert response.context["submission_activity_approved"] == [
        "高二2班的资料已经审核",
    ]
    body = response.content.decode()
    assert body.count("高三1班上传了资料") == 1
    assert body.count("高二2班的资料已经审核") == 1
    assert "初三3班" not in body
    assert "2026" not in response.context["submission_activity_uploaded"][0]
    assert "高中部" not in response.context["submission_activity_uploaded"][0]


@pytest.mark.django_db
def test_public_home_activity_template_is_progressive_and_cache_versioned(client):
    response = client.get(reverse("submit"))
    body = response.content.decode()

    assert response.status_code == 200
    assert "submission_activity.css?v=20261113-1" in body
    assert "submission_activity.js?v=20261116-2" not in body

    school_year = SchoolYear.objects.create(name="2026")
    class_group = ClassGroup.objects.create(department="high", school_year=school_year, grade="高一", name="1")
    make_submission(class_group, "pending", "template-check")
    body = client.get(reverse("submit")).content.decode()
    assert "submission_activity.js?v=20261116-2" in body
    assert "data-submission-activity" in body


@pytest.mark.django_db
def test_public_home_limits_each_activity_list_to_ten_unique_classes(client):
    school_year = SchoolYear.objects.create(name="2026")
    for index in range(11):
        class_group = ClassGroup.objects.create(
            department="high",
            school_year=school_year,
            grade="高一",
            name=str(index + 1),
        )
        make_submission(class_group, "approved" if index == 0 else "pending", f"limit-{index}")

    response = client.get(reverse("submit"))

    assert len(response.context["submission_activity_uploaded"]) == 10
    assert len(response.context["submission_activity_approved"]) == 1
