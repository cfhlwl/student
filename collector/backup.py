"""Consistent SQLite/media backups and recoverable collection clearing."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from django.conf import settings
from django.db import transaction

from .models import AppSetting, AuditLog, ClassGroup, NamingRule, PhotoSupplement, SchoolYear, StagedUpload, Student, StudentPhoto, Submission, SubmissionDocument, UploadSession
from .maintenance import collection_maintenance_gate, write_state


BACKUP_FORMAT = "student-collector-backup-v2"
COLLECTION_MEDIA_DIRS = ("submissions", "upload_sessions", "supplements")


class BackupError(RuntimeError):
    pass


def _replace_directory_contents(source: Path, target: Path) -> None:
    """Replace a directory's contents without removing a Docker mountpoint."""
    target.mkdir(parents=True, exist_ok=True)
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    if source.exists():
        for child in source.iterdir():
            destination = target / child.name
            if child.is_dir() and not child.is_symlink():
                shutil.copytree(child, destination)
            else:
                shutil.copy2(child, destination)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_sqlite_online(source: Path | None, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source is None:
        from django.db import connection as django_connection
        django_connection.ensure_connection()
        source_connection = django_connection.connection
        owns_source = False
    else:
        source_connection = sqlite3.connect(str(source), timeout=30)
        owns_source = True
    target_connection = sqlite3.connect(str(target), timeout=30)
    try:
        if hasattr(source_connection, "commit"):
            source_connection.commit()
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        if owns_source:
            source_connection.close()


def _media_files(media_root: Path):
    if not media_root.exists():
        return
    for root, _, names in os.walk(media_root):
        for name in sorted(names):
            path = Path(root) / name
            relative = path.relative_to(media_root).as_posix()
            if relative.startswith(".clear-quarantine-"):
                continue
            yield path, relative


def _manifest(database: Path, media_root: Path, created_at: str) -> dict:
    files = []
    for path, relative in _media_files(media_root) or ():
        files.append({"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return {"format": BACKUP_FORMAT, "created_at": created_at, "database": {"path": "db.sqlite3", "size": database.stat().st_size, "sha256": sha256_file(database)}, "media": files}


def create_backup(output: str | Path, *, database_path: str | Path | None = None, media_root: str | Path | None = None, assume_maintenance_lock: bool = False) -> Path:
    if assume_maintenance_lock:
        return _create_backup_unlocked(output, database_path=database_path, media_root=media_root)
    with collection_maintenance_gate():
        return _create_backup_unlocked(output, database_path=database_path, media_root=media_root)


def _create_backup_unlocked(output: str | Path, *, database_path: str | Path | None = None, media_root: str | Path | None = None) -> Path:
    database_name = str(database_path or settings.DATABASES["default"]["NAME"])
    database = Path(database_name).resolve() if not database_name.startswith("file:") else None
    media = Path(media_root or settings.MEDIA_ROOT).resolve()
    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise BackupError(f"备份文件已存在，拒绝覆盖：{destination.name}")
    if database is not None and not database.exists():
        raise BackupError(f"数据库不存在：{database}")
    staging = Path(tempfile.mkdtemp(prefix="collection-backup-", dir=destination.parent))
    partial = destination.with_name(destination.name + f".partial-{uuid.uuid4().hex}")
    try:
        staged_db = staging / "db.sqlite3"
        _copy_sqlite_online(database, staged_db)
        staged_media = staging / "media"
        if media.exists():
            shutil.copytree(media, staged_media, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".clear-quarantine-*"))
        created_at = datetime.now(timezone.utc).isoformat()
        manifest = _manifest(staged_db, staged_media, created_at)
        (staging / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with ZipFile(partial, "w", ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(staged_db, "db.sqlite3")
            for path, relative in _media_files(staged_media) or ():
                archive.write(path, f"media/{relative}")
            archive.write(staging / "MANIFEST.json", "MANIFEST.json")
        verify_backup(partial)
        try:
            os.link(partial, destination)
        except FileExistsError as exc:
            raise BackupError(f"备份文件已存在，拒绝覆盖：{destination.name}") from exc
        partial.unlink(missing_ok=True)
        return destination
    except Exception as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(str(exc)) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _read_verified_archive(archive_path: str | Path, staging: Path) -> dict:
    try:
        with ZipFile(archive_path) as archive:
            bad = archive.testzip()
            if bad:
                raise BackupError(f"ZIP CRC 校验失败：{bad}")
            names = set(archive.namelist())
            if "MANIFEST.json" not in names or "db.sqlite3" not in names:
                raise BackupError("备份缺少 MANIFEST.json 或 db.sqlite3")
            for name in names:
                if name.startswith("/") or ".." in Path(name).parts:
                    raise BackupError(f"备份路径非法：{name}")
            archive.extractall(staging)
    except (BadZipFile, OSError) as exc:
        raise BackupError(str(exc)) from exc
    manifest = json.loads((staging / "MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupError("不支持的备份格式")
    database = staging / "db.sqlite3"
    expected = manifest.get("database", {})
    if database.stat().st_size != expected.get("size") or sha256_file(database) != expected.get("sha256"):
        raise BackupError("备份数据库清单校验失败")
    for item in manifest.get("media", []):
        path = staging / "media" / item["path"]
        if not path.is_file() or path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
            raise BackupError(f"备份媒体清单校验失败：{item['path']}")
    return manifest


def verify_backup(archive_path: str | Path) -> dict:
    staging = Path(tempfile.mkdtemp(prefix="verify-backup-", dir=Path(archive_path).resolve().parent))
    try:
        return _read_verified_archive(Path(archive_path), staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def restore_backup(archive_path: str | Path, *, database_path: str | Path | None = None, media_root: str | Path | None = None) -> dict:
    with collection_maintenance_gate():
        return _restore_backup_unlocked(archive_path, database_path=database_path, media_root=media_root)


def _restore_backup_unlocked(archive_path: str | Path, *, database_path: str | Path | None = None, media_root: str | Path | None = None) -> dict:
    database = Path(database_path or settings.DATABASES["default"]["NAME"]).resolve()
    media = Path(media_root or settings.MEDIA_ROOT).resolve()
    staging = Path(tempfile.mkdtemp(prefix="restore-backup-", dir=Path(archive_path).resolve().parent))
    rollback = Path(tempfile.mkdtemp(prefix="restore-rollback-", dir=staging.parent))
    old_db = rollback / "db.sqlite3"
    old_media = rollback / "media"
    temp_media = None
    try:
        manifest = _read_verified_archive(Path(archive_path), staging)
        if database.exists():
            shutil.copy2(database, old_db)
        if media.exists():
            shutil.copytree(media, old_media, dirs_exist_ok=True)
        database.parent.mkdir(parents=True, exist_ok=True)
        temp_db = database.with_name(database.name + f".restore-{uuid.uuid4().hex}")
        shutil.copy2(staging / "db.sqlite3", temp_db)
        os.replace(temp_db, database)
        temp_media = media.with_name(media.name + f".restore-{uuid.uuid4().hex}")
        if temp_media.exists():
            shutil.rmtree(temp_media)
        if (staging / "media").exists():
            shutil.copytree(staging / "media", temp_media)
        else:
            temp_media.mkdir(parents=True)
        _replace_directory_contents(temp_media, media)
        shutil.rmtree(temp_media, ignore_errors=True)
        return manifest
    except Exception as exc:
        if old_db.exists():
            shutil.copy2(old_db, database)
        if old_media.exists():
            _replace_directory_contents(old_media, media)
        raise BackupError(str(exc)) from exc
    finally:
        if temp_media is not None:
            shutil.rmtree(temp_media, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(rollback, ignore_errors=True)


def clear_collection_safely(backup_path: str | Path, *, database_path: str | Path | None = None, media_root: str | Path | None = None) -> dict:
    """Freeze all collection writes, back up, then clear without reopening."""
    database_name = str(database_path or settings.DATABASES["default"]["NAME"])
    database = Path(database_name).resolve() if not database_name.startswith("file:") else None
    media = Path(media_root or settings.MEDIA_ROOT).resolve()
    with collection_maintenance_gate() as previous_state:
        previous_gate = AppSetting.get_value("public_upload_open", "__missing__")
        create_backup(backup_path, database_path=database, media_root=media, assume_maintenance_lock=True)
        backup_created = True
        AppSetting.set_value("public_upload_open", "false")
        quarantine = media / f".clear-quarantine-{uuid.uuid4().hex}"
        moved = []
        try:
            quarantine.mkdir(parents=True, exist_ok=False)
            for name in COLLECTION_MEDIA_DIRS:
                root = media / name
                if root.exists():
                    target = quarantine / name
                    shutil.move(str(root), str(target))
                    moved.append((root, target))
            with transaction.atomic():
                counts = {"submissions": Submission.objects.count(), "students": Student.objects.count(), "photos": StudentPhoto.objects.count(), "documents": SubmissionDocument.objects.count(), "rosters": sum(bool(row.roster_file) for row in Submission.objects.all()), "staged_uploads": StagedUpload.objects.count(), "upload_sessions": UploadSession.objects.count(), "supplements": PhotoSupplement.objects.count()}
                Submission.objects.all().delete()
                Student.objects.all().delete()
                PhotoSupplement.objects.all().delete()
                UploadSession.objects.all().delete()
                StagedUpload.objects.all().delete()
                ClassGroup.objects.all().delete()
                SchoolYear.objects.all().delete()
                NamingRule.objects.all().delete()
                AppSetting.objects.all().delete()
                AuditLog.objects.all().delete()
            for name in COLLECTION_MEDIA_DIRS:
                (media / name).mkdir(parents=True, exist_ok=True)
            write_state("closed")
            shutil.rmtree(quarantine, ignore_errors=True)
            return counts
        except Exception:
            restored_from_backup = False
            if locals().get("backup_created"):
                try:
                    # If a post-transaction filesystem operation fails, the
                    # already-verified snapshot restores both SQLite and all
                    # media instead of leaving a half-cleared collection.
                    restore_backup(backup_path, database_path=database, media_root=media)
                    restored_from_backup = True
                except Exception:
                    restored_from_backup = False
            if not restored_from_backup:
                if previous_gate == "__missing__":
                    AppSetting.objects.filter(key="public_upload_open").delete()
                else:
                    AppSetting.set_value("public_upload_open", previous_gate)
                for root, target in reversed(moved):
                    if root.exists():
                        shutil.rmtree(root, ignore_errors=True)
                    if target.exists():
                        shutil.move(str(target), str(root))
            write_state(previous_state)
            shutil.rmtree(quarantine, ignore_errors=True)
            raise
