import io

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook
from PIL import Image

from collector.models import AppSetting
from collector.photo_quality import PhotoQualityResult
from collector.services import ValidationError, create_submission


def roster_upload():
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型"])
    sheet.append(["测试学生", "男", "高中部", "2026", "高一", "1", "走读"])
    data = io.BytesIO()
    book.save(data)
    return SimpleUploadedFile("名单.xlsx", data.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def photo_upload():
    data = io.BytesIO()
    Image.new("RGB", (160, 160), (120, 120, 120)).save(data, format="JPEG")
    return SimpleUploadedFile("测试学生.jpg", data.getvalue(), content_type="image/jpeg")


@pytest.mark.django_db
def test_admin_can_toggle_photo_quality_blocking(client):
    admin = User.objects.create_user(username="quality-admin", password="strong-password-123", is_staff=True)
    client.force_login(admin)
    AppSetting.set_value("photo_face_quality_detection_enabled", True)

    response = client.post(reverse("toggle-photo-quality"))

    assert response.status_code == 302
    assert AppSetting.get_bool("photo_face_quality_blocking") is True


@pytest.mark.django_db
def test_blocking_mode_rejects_failed_quality_before_persisting(monkeypatch):
    AppSetting.set_value("photo_face_quality_blocking", True)
    failed = PhotoQualityResult("failed", "face_too_small", "人脸区域过小")
    monkeypatch.setattr("collector.services.assess_photo_quality", lambda upload: failed)

    with pytest.raises(ValidationError, match="人脸质量检查"):
        create_submission(roster_upload=roster_upload(), photo_uploads=[photo_upload()], document_uploads=[])
