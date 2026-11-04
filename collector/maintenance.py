"""Cross-worker maintenance and collection write coordination.

The collection uses SQLite and private media files.  A shared file lock drains
all ordinary writes before a backup/clear operation takes its exclusive lock.
The small state file remains available while the database is being reset, so a
successful clear cannot accidentally reopen public collection when settings
rows are removed.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from django.conf import settings
from .models import AppSetting

try:  # pragma: no cover - exercised in the Linux Docker image
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None


class MaintenanceError(RuntimeError):
    """Raised when collection writes are closed for maintenance."""


_fallback_lock = threading.RLock()
_local = threading.local()


def _lock_path() -> Path:
    return Path(getattr(settings, "COLLECTION_MAINTENANCE_LOCK_PATH", settings.BASE_DIR / "data" / "collection-maintenance.lock"))


def _state_path() -> Path:
    return Path(getattr(settings, "COLLECTION_MAINTENANCE_STATE_PATH", settings.BASE_DIR / "data" / "collection-maintenance.json"))


def read_state() -> str:
    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return "open"
    return payload.get("state", "open") if payload.get("state") in {"open", "closed", "clearing"} else "open"


def write_state(state: str) -> None:
    if state not in {"open", "closed", "clearing"}:
        raise ValueError("invalid maintenance state")
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    temporary.write_text(json.dumps({"state": state}, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def public_upload_is_open() -> bool:
    """Return the effective public-upload state from both persistent gates."""
    return read_state() == "open" and AppSetting.get_bool("public_upload_open", True)


@contextmanager
def _file_lock(*, exclusive: bool):
    depth = getattr(_local, "depth", 0)
    if depth:
        yield
        return
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        _local.depth = 1
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            else:
                _fallback_lock.acquire()
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                _fallback_lock.release()
            _local.depth = 0


@contextmanager
def collection_write_gate():
    """Allow one normal collection write while maintenance is open."""
    with _file_lock(exclusive=False):
        if read_state() != "open":
            raise MaintenanceError("采集资料正在维护或已关闭，请联系管理员开启后重试")
        yield


@contextmanager
def collection_maintenance_gate():
    """Take the cross-worker exclusive gate for backup/clear/restore."""
    with _file_lock(exclusive=True):
        previous = read_state()
        write_state("clearing")
        try:
            yield previous
        finally:
            # A normal backup restores the prior state.  A clear operation
            # explicitly writes ``closed`` before leaving.  If the process is
            # interrupted, the on-disk ``clearing`` marker remains and blocks
            # writes conservatively until an operator completes recovery.
            if read_state() == "clearing":
                write_state(previous)


def collection_maintenance_required(function):
    """Run a destructive collection reset under the exclusive gate."""
    @wraps(function)
    def guarded(*args, **kwargs):
        with collection_maintenance_gate():
            result = function(*args, **kwargs)
            write_state("closed")
            return result
    return guarded

