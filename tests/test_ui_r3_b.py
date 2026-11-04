import hashlib
import io

import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from openpyxl import Workbook
from PIL import Image

from collector.models import PhotoSupplement, SupplementPhoto
from collector.services import create_submission


def roster_upload(name="界面测试名单.xlsx"):
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    sheet.append(["界面同学", "男", "高中部", "2026", "UI-1", "走读", ""])
    data = io.BytesIO()
    book.save(data)
    return SimpleUploadedFile(
        name,
        data.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _submission(*, photos=False):
    uploads = []
    if photos:
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(image, format="JPEG")
        uploads.append(SimpleUploadedFile("界面同学.jpg", image.getvalue(), content_type="image/jpeg"))
    return create_submission(roster_upload=roster_upload(), photo_uploads=uploads, document_uploads=[])


@pytest.mark.django_db
def test_success_page_exposes_copy_controls_and_save_hint(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client = Client()
    response = client.post(
        reverse("submit"),
        data={"roster": roster_upload(), "note": "界面测试"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "data-copy-target" in body
    assert "复制提交编号" in body
    assert "复制修改凭证" in body
    assert "复制结果链接" in body
    assert "保存修改凭证" in body
    assert "clipboard.js" in body


@pytest.mark.django_db
def test_result_page_exposes_summary_and_status_filter(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission, token = _submission()
    response = Client().get(
        reverse("submit-result", args=[submission.id]),
        {"token": token},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "result-summary" in body
    assert "学生总数" in body
    assert "已匹配" in body
    assert "缺少照片" in body
    assert "待人工确认" in body
    assert "data-result-filter" in body
    assert "data-match-status" in body
    assert "clipboard.js" in body
    assert "补传缺失照片" in body


@pytest.mark.django_db
def test_result_page_shows_success_and_hides_supplement_when_all_photos_match(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission, token = _submission(photos=True)
    response = Client().get(reverse("submit-result", args=[submission.id]), {"token": token})

    assert response.status_code == 200
    body = response.content.decode()
    assert "提交成功！" in body
    assert "照片已全部匹配" in body
    assert "补传缺失照片" not in body
    assert "待审核" in body


@pytest.mark.django_db
def test_result_page_keeps_supplement_for_ambiguous_match(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission, token = _submission()
    student = submission.students.get()
    student.match_status = "ambiguous"
    student.save(update_fields=["match_status"])
    body = Client().get(reverse("submit-result", args=[submission.id]), {"token": token}).content.decode()

    assert "补传缺失照片" in body
    assert "提交成功！" not in body


@pytest.mark.django_db
def test_result_page_rejects_wrong_token(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission, _ = _submission(photos=True)
    response = Client().get(reverse("submit-result", args=[submission.id]), {"token": "wrong-token"})

    assert response.status_code == 404


@pytest.mark.django_db
def test_admin_review_page_exposes_pre_review_summary_and_safe_clear_action(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    submission, _ = _submission()
    supplement = PhotoSupplement.objects.create(submission=submission)
    payload = b"supplement-image"
    SupplementPhoto.objects.create(
        supplement=supplement,
        original_name="待确认.jpg",
        file=ContentFile(payload, name="待确认.jpg"),
        content_type="image/jpeg",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    admin = User.objects.create_user(
        username="ui-admin",
        password="ui-admin-password-strong",
        is_staff=True,
    )
    client = Client()
    assert client.login(username=admin.username, password="ui-admin-password-strong")

    response = client.get(reverse("admin-submissions"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "审核前摘要" in body
    assert "学生人数" in body
    assert "照片数量" in body
    assert "缺漏人数" in body
    assert "异常数量" in body
    assert "补传状态" in body

    dashboard = client.get(reverse("admin-dashboard"))
    dashboard_body = dashboard.content.decode()
    assert reverse("clear-test-data") in dashboard_body
    assert "清空全部采集数据" in dashboard_body


def test_ui_clipboard_and_filter_script_contract():
    source = open("static/js/clipboard.js", encoding="utf-8").read()
    assert "navigator.clipboard.writeText" in source
    assert "data-copy-target" in source
    assert "data-result-filter" in source
