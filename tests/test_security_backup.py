import hashlib
import sqlite3
from pathlib import Path

import pytest
from django.core.cache import caches
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from collector.backup import clear_collection_safely, create_backup, restore_backup, verify_backup
from collector.middleware import CollectionRateLimitMiddleware, TrustedProxyMiddleware
from collector.models import ClassGroup, SchoolYear, Submission


def test_trusted_proxy_headers_are_used_only_from_configured_peer(settings):
    captured = []

    def response(request):
        captured.append(request.META.copy())
        return HttpResponse(request.META["REMOTE_ADDR"])

    settings.TRUSTED_PROXY_NETWORKS = ("127.0.0.1/32",)
    middleware = TrustedProxyMiddleware(response)
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.8, 127.0.0.1", HTTP_X_REAL_IP="203.0.113.9")
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    assert middleware(request).content == b"203.0.113.8"

    direct = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.8", HTTP_X_REAL_IP="203.0.113.9")
    direct.META["REMOTE_ADDR"] = "198.51.100.4"
    assert middleware(direct).content == b"198.51.100.4"
    assert "HTTP_X_FORWARDED_FOR" not in captured[-1]


@override_settings(SECURITY_RATE_LIMITS={"upload_start": (2, 60)})
def test_upload_rate_limit_uses_shared_file_cache_not_sqlite():
    cache = caches["security_rate"]
    cache.clear()
    middleware = CollectionRateLimitMiddleware(lambda request: HttpResponse("ok"))
    factory = RequestFactory()
    responses = []
    for _ in range(3):
        request = factory.post("/submit/upload/start/")
        request.META["REMOTE_ADDR"] = "203.0.113.10"
        responses.append(middleware(request))
    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1]["Cache-Control"] == "no-store"


def _raw_database(path: Path):
    connection = sqlite3.connect(path)
    connection.execute("create table sample (value text)")
    connection.execute("insert into sample values (?)", ("虚构备份数据",))
    connection.commit()
    connection.close()


def test_backup_manifest_crc_sha256_and_isolated_restore(tmp_path):
    database = tmp_path / "source.sqlite3"
    media = tmp_path / "media"
    media.mkdir()
    (media / "submissions").mkdir()
    payload = b"fictional-student-photo"
    (media / "submissions" / "photo.jpg").write_bytes(payload)
    _raw_database(database)
    backup = tmp_path / "collection.zip"

    created = create_backup(backup, database_path=database, media_root=media)
    manifest = verify_backup(created)
    assert manifest["format"] == "student-collector-backup-v2"
    assert manifest["media"][0]["sha256"] == hashlib.sha256(payload).hexdigest()

    restored_db = tmp_path / "restored.sqlite3"
    restored_media = tmp_path / "restored-media"
    restore_backup(created, database_path=restored_db, media_root=restored_media)
    restored = sqlite3.connect(restored_db)
    assert restored.execute("select value from sample").fetchone() == ("虚构备份数据",)
    restored.close()
    assert (restored_media / "submissions" / "photo.jpg").read_bytes() == payload


@pytest.mark.django_db
def test_safe_clear_requires_backup_and_preserves_admin(settings, tmp_path):
    media = tmp_path / "media"
    settings.MEDIA_ROOT = media
    admin_user = User.objects.create_user(username="backup-admin", password="strong-password-123", is_staff=True)
    year = SchoolYear.objects.create(name="2099")
    group = ClassGroup.objects.create(department="high", school_year=year, grade="高一", name="备份测试班")
    Submission.objects.create(
        access_token_hash="a" * 64, department="high", school_year=year, grade="高一",
        class_group=group, source_filename="虚构.xlsx",
    )
    (media / "submissions").mkdir(parents=True)
    (media / "submissions" / "fictional.jpg").write_bytes(b"fictional")
    backup = tmp_path / "before-clear.zip"

    counts = clear_collection_safely(backup, media_root=media)
    assert counts["submissions"] == 1
    assert Submission.objects.count() == 0
    assert ClassGroup.objects.count() == 0
    assert admin_user.__class__.objects.filter(pk=admin_user.pk).exists()
    assert verify_backup(backup)["media"]
    assert list((media / "submissions").iterdir()) == []
