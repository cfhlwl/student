import io
import json
import threading
import time
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from openpyxl import load_workbook

from collector.backup import clear_collection_safely, create_backup
from collector.maintenance import MaintenanceError, collection_write_gate, read_state, write_state
from collector.models import AppSetting, Submission
from collector.security_controls import classify_rate_limit, consume_rate_limit
from collector.services import create_submission
from tests.test_core import excel_upload, image_upload


@pytest.mark.django_db
def test_clear_requires_current_admin_password_without_mutation(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.COLLECTION_BACKUP_DIR = tmp_path / "backups"
    admin = User.objects.create_user(username="f1-admin", password="correct-password-123", is_staff=True)
    create_submission(roster_upload=excel_upload([["F1同学", "男", "高中部", "2026", "1", "走读", ""]]), photo_uploads=[image_upload("F1同学.jpg")], document_uploads=[])
    assert client.login(username=admin.username, password="correct-password-123")
    for payload in ({"confirm_clear": "YES"}, {"confirm_clear": "YES", "admin_password": "wrong-password"}):
        response = client.post(reverse("clear-test-data"), payload)
        assert response.status_code == 302
        assert Submission.objects.count() == 1
        assert not list((settings.COLLECTION_BACKUP_DIR).glob("*.zip"))
    response = client.post(reverse("clear-test-data"), {"confirm_clear": "YES", "admin_password": "correct-password-123"})
    assert response.status_code == 302
    assert Submission.objects.count() == 0
    assert User.objects.filter(pk=admin.pk).exists()
    assert read_state() == "closed"


@pytest.mark.django_db
def test_clear_uses_unique_backup_and_requires_explicit_reopen(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.COLLECTION_BACKUP_DIR = tmp_path / "backups"
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    clear_collection_safely(first, media_root=settings.MEDIA_ROOT)
    assert read_state() == "closed"
    clear_collection_safely(second, media_root=settings.MEDIA_ROOT)
    assert first.exists() and second.exists() and first.read_bytes() != second.read_bytes()
    with pytest.raises(MaintenanceError):
        with collection_write_gate():
            pass
    write_state("open")
    with collection_write_gate():
        pass


@pytest.mark.django_db
def test_clear_failure_restores_database_and_media(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path / "media"
    submission, _ = create_submission(
        roster_upload=excel_upload([["回滚同学", "男", "高中部", "2026", "1", "走读", ""]]),
        photo_uploads=[image_upload("回滚同学.jpg")], document_uploads=[],
    )
    original = next(settings.MEDIA_ROOT.rglob("*.jpg")).read_bytes()
    import collector.backup as backup_module
    original_move = backup_module.shutil.move

    def fail_first_move(source, destination):
        monkeypatch.setattr(backup_module.shutil, "move", original_move)
        raise OSError("injected quarantine failure")

    monkeypatch.setattr(backup_module.shutil, "move", fail_first_move)
    with pytest.raises(OSError):
        clear_collection_safely(tmp_path / "rollback.zip", media_root=settings.MEDIA_ROOT)
    assert Submission.objects.filter(pk=submission.pk).exists()
    assert original in [path.read_bytes() for path in settings.MEDIA_ROOT.rglob("*.jpg")]
    assert read_state() == "open"


def test_exclusive_maintenance_gate_waits_for_existing_writer(settings, tmp_path):
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = tmp_path / "maintenance.lock"
    settings.COLLECTION_MAINTENANCE_STATE_PATH = tmp_path / "maintenance.json"
    entered = threading.Event()
    release = threading.Event()
    acquired = threading.Event()

    def writer():
        with collection_write_gate():
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=writer)
    thread.start()
    assert entered.wait(2)

    from collector.maintenance import collection_maintenance_gate
    def clearer():
        with collection_maintenance_gate():
            acquired.set()

    clear_thread = threading.Thread(target=clearer)
    clear_thread.start()
    time.sleep(0.1)
    assert not acquired.is_set()
    release.set()
    thread.join(3)
    clear_thread.join(3)
    assert acquired.is_set()


def test_existing_backup_is_never_overwritten(tmp_path):
    database = tmp_path / "db.sqlite3"
    database.write_bytes(b"not-a-real-sqlite")
    output = tmp_path / "backup.zip"
    output.write_bytes(b"sentinel")
    with pytest.raises(Exception):
        create_backup(output, database_path=database, media_root=tmp_path / "media")
    assert output.read_bytes() == b"sentinel"


def test_preflight_has_dedicated_bounded_quota_and_nginx_overwrites_xff():
    assert classify_rate_limit("/submit/preflight/check/", "POST") == "preflight"
    for index in range(120):
        allowed, _ = consume_rate_limit(name="preflight", client_ip="203.0.113.55", now=10)
        assert allowed
    allowed, _ = consume_rate_limit(name="preflight", client_ip="203.0.113.55", now=10)
    assert not allowed
    nginx = Path("deploy/nginx.conf").read_text(encoding="utf-8")
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx


@pytest.mark.django_db
def test_preflight_continue_returns_to_submit_with_reselect_notice(client):
    AppSetting.set_value("public_upload_open", True)
    response = client.get(reverse("submit-preflight-page"))
    body = response.content.decode()
    assert 'data-preflight-continue' in body
    assert 'data-submit-url="/submit"' in body
    script = Path("static/js/preflight.js").read_text(encoding="utf-8")
    assert "window.location.assign" in script
    follow = client.get(reverse("submit") + "?from=preflight")
    assert "请重新选择 Excel、照片和相关文档" in follow.content.decode()
