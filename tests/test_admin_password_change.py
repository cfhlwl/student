import pytest

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from collector.models import AuditLog


OLD_PASSWORD = "Old-Staff-Password-2026!"
NEW_PASSWORD = "New-Staff-Password-2026!"


def _staff(username="password-admin"):
    return User.objects.create_user(username=username, password=OLD_PASSWORD, is_staff=True)


def _password_data(old=OLD_PASSWORD, new=NEW_PASSWORD, confirm=NEW_PASSWORD):
    return {"old_password": old, "new_password1": new, "new_password2": confirm}


@pytest.mark.django_db
def test_password_change_requires_staff_and_has_sidebar_entry(client):
    url = reverse("admin-password-change")
    anonymous = client.get(url)
    assert anonymous.status_code == 302
    assert "/admin/login/" in anonymous["Location"]

    ordinary_user = User.objects.create_user(username="non-staff", password=OLD_PASSWORD)
    client.force_login(ordinary_user)
    assert client.get(url).status_code == 403
    assert client.post(url, _password_data()).status_code == 403

    admin = _staff()
    client.force_login(admin)
    response = client.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "修改登录密码" in body
    assert 'type="password"' in body
    assert 'name="old_password"' in body
    assert 'name="new_password1"' in body
    assert 'name="new_password2"' in body
    assert f'href="{url}"' in body
    assert "no-store" in response["Cache-Control"]


@pytest.mark.django_db
def test_password_change_rejects_invalid_password_input(client):
    admin = _staff()
    client.force_login(admin)
    url = reverse("admin-password-change")

    for data in (
        _password_data(old="Incorrect-Password-2026!"),
        _password_data(confirm="Mismatch-Password-2026!"),
        _password_data(new="short", confirm="short"),
        _password_data(new=OLD_PASSWORD, confirm=OLD_PASSWORD),
    ):
        response = client.post(url, data)
        assert response.status_code == 200
        admin.refresh_from_db()
        assert admin.check_password(OLD_PASSWORD)
        assert not AuditLog.objects.filter(action="admin_password_change").exists()


@pytest.mark.django_db
def test_password_change_keeps_current_session_and_revokes_old_password(client):
    admin = _staff()
    client.force_login(admin)
    other_session = Client()
    other_session.force_login(admin)
    url = reverse("admin-password-change")

    response = client.post(url, _password_data())
    assert response.status_code == 302
    assert response["Location"] == url

    admin.refresh_from_db()
    assert admin.check_password(NEW_PASSWORD)
    assert not admin.check_password(OLD_PASSWORD)
    assert client.get(reverse("admin-dashboard")).status_code == 200
    assert other_session.get(reverse("admin-dashboard")).status_code == 302

    audit = AuditLog.objects.get(action="admin_password_change")
    assert audit.actor_id == admin.pk
    assert audit.target_id == str(admin.pk)
    assert audit.detail == {}
    assert OLD_PASSWORD not in str(audit.detail)
    assert NEW_PASSWORD not in str(audit.detail)

    client.logout()
    assert not client.login(username=admin.username, password=OLD_PASSWORD)
    assert client.login(username=admin.username, password=NEW_PASSWORD)


@pytest.mark.django_db
def test_password_change_is_csrf_protected():
    admin = _staff()
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin)
    url = reverse("admin-password-change")
    assert client.get(url, secure=True).status_code == 200

    assert client.post(url, _password_data(), secure=True).status_code == 403
    admin.refresh_from_db()
    assert admin.check_password(OLD_PASSWORD)

    token = client.cookies["csrftoken"].value
    response = client.post(url, _password_data(), HTTP_X_CSRFTOKEN=token, secure=True)
    assert response.status_code == 302
    admin.refresh_from_db()
    assert admin.check_password(NEW_PASSWORD)
