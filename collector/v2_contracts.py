"""Deterministic helpers for the V2 shared P0 contract.

This module deliberately contains no view or UI code.  Later work lines use
these helpers instead of re-implementing version, schema, or claim semantics.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction


ALLOWED_FIELD_TYPES = {"text", "integer", "decimal", "date", "choice", "boolean", "image_key", "attachment"}
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
NAMESPACE_KINDS = {"project", "scope", "template"}

RUN_TRANSITIONS = {
    "uploading": {"finalizing", "abandoned", "expired"},
    "finalizing": {"submitted", "abandoned", "expired"},
    "submitted": {"reviewing", "rejected", "abandoned", "expired"},
    "reviewing": {"adopted", "rejected", "abandoned", "expired"},
    "adopted": {"superseded"},
    "superseded": set(),
    "rejected": set(),
    "abandoned": set(),
    "expired": set(),
}

ACTIVITY_TRANSITIONS = {
    "draft": {"open", "closed"},
    "open": {"paused", "closed"},
    "paused": {"open", "closed"},
    "closed": {"archived"},
    "archived": set(),
}


class UniqueClaimConflict(ValidationError):
    """A deterministic row-level validation error for an active claim conflict."""


def stable_json_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonicalize_unique_value(value: object, *, case_sensitive: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, Decimal)):
        text = str(value)
    elif isinstance(value, float):
        text = format(Decimal(str(value)), "f")
    elif isinstance(value, (date, datetime)):
        text = value.isoformat()
    else:
        text = str(value)
    text = unicodedata.normalize("NFKC", text)
    text = " ".join(text.strip().split())
    if not case_sensitive:
        text = text.casefold()
    return text


def canonicalize_scope_key(values: dict[str, object], fields: list[str]) -> str:
    parts = []
    for field in fields:
        value = canonicalize_unique_value(values.get(field), case_sensitive=True)
        parts.append(f"{len(value)}:{value}")
    return "|".join(parts)


def validate_template_schema(schema: object) -> dict:
    if not isinstance(schema, dict):
        raise ValidationError("模板 Schema 必须是对象")
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValidationError("模板 Schema 必须包含非空 fields 列表")
    seen: set[str] = set()
    for index, field in enumerate(fields, start=1):
        if not isinstance(field, dict):
            raise ValidationError(f"第 {index} 个字段不是对象")
        key = field.get("key")
        if not isinstance(key, str) or not FIELD_KEY_RE.fullmatch(key):
            raise ValidationError(f"第 {index} 个字段 key 无效")
        if key in seen:
            raise ValidationError(f"字段 key 重复：{key}")
        seen.add(key)
        field_type = field.get("type")
        if field_type not in ALLOWED_FIELD_TYPES:
            raise ValidationError(f"字段 {key} 的 type 不受支持")
        if not isinstance(field.get("label"), str) or not field["label"].strip():
            raise ValidationError(f"字段 {key} 缺少 label")
        if field_type == "choice" and not isinstance(field.get("choices"), list):
            raise ValidationError(f"choice 字段 {key} 必须提供 choices")
        if "order" in field and (not isinstance(field["order"], int) or field["order"] < 0):
            raise ValidationError(f"字段 {key} 的 order 无效")
        if "unique" in field and not isinstance(field["unique"], bool):
            raise ValidationError(f"字段 {key} 的 unique 必须是布尔值")
    return schema


def build_export_snapshot(template_version, config_version) -> dict:
    """Return the immutable joint export snapshot stored on a CollectionRun."""
    return {
        "template": {
            "id": template_version.pk,
            "version": template_version.version,
            "schema_sha256": template_version.schema_sha256,
            "field_export": template_version.field_export_json,
        },
        "config": {
            "id": config_version.pk,
            "version": config_version.version,
            "settings_sha256": config_version.settings_sha256,
            "export_policy": config_version.export_policy_json,
        },
    }


def assert_transition(kind: str, current: str, target: str) -> None:
    table = {"run": RUN_TRANSITIONS, "activity": ACTIVITY_TRANSITIONS}.get(kind)
    if table is None or target not in table.get(current, set()):
        raise ValidationError(f"不允许的 {kind} 状态转换：{current} -> {target}")


def claim_unique_value(*, project, field_key: str, namespace_kind: str, namespace_key: str, value: object, owner_identity, source_record=None, case_sensitive: bool = False):
    """Create one active claim or raise a deterministic validation conflict."""
    for attempt in range(4):
        try:
            return _claim_unique_value_once(
                project=project,
                field_key=field_key,
                namespace_kind=namespace_kind,
                namespace_key=namespace_key,
                value=value,
                owner_identity=owner_identity,
                source_record=source_record,
                case_sensitive=case_sensitive,
            )
        except OperationalError as exc:
            # A short SQLite busy window is expected under two real workers;
            # retry outside the rolled-back transaction rather than leaking a
            # database-lock HTTP 500 to the importer.
            if "locked" not in str(exc).lower() or attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


@transaction.atomic
def _claim_unique_value_once(*, project, field_key: str, namespace_kind: str, namespace_key: str, value: object, owner_identity, source_record=None, case_sensitive: bool = False):
    """Single short transaction used by claim_unique_value's retry loop."""
    from .models import RecordUniqueValue

    if namespace_kind not in NAMESPACE_KINDS:
        raise ValidationError("不支持的唯一值命名空间")
    canonical = canonicalize_unique_value(value, case_sensitive=case_sensitive)
    if not canonical:
        return None
    # SQLite does not implement row locks for select_for_update, so the
    # partial unique index remains the final arbiter under concurrent writers.
    type(project).objects.select_for_update().get(pk=project.pk)
    owner_identity.__class__.objects.select_for_update().get(pk=owner_identity.pk)
    existing = RecordUniqueValue.objects.filter(
        project=project,
        field_key=field_key,
        namespace_kind=namespace_kind,
        namespace_key=namespace_key,
        canonical_value=canonical,
        is_active=True,
    ).first()
    if existing:
        if existing.owner_identity_id == owner_identity.pk:
            return existing
        raise UniqueClaimConflict(f"唯一字段 {field_key} 的值已被其他记录占用")
    try:
        return RecordUniqueValue.objects.create(
            project=project,
            field_key=field_key,
            namespace_kind=namespace_kind,
            namespace_key=namespace_key,
            canonical_value=canonical,
            owner_identity=owner_identity,
            source_record=source_record,
            is_active=True,
        )
    except IntegrityError as exc:
        raise UniqueClaimConflict(f"唯一字段 {field_key} 的值已被其他记录占用") from exc


def release_unique_claims(*, owner_identity, source_record=None) -> int:
    from django.utils import timezone
    from .models import RecordUniqueValue

    claims = RecordUniqueValue.objects.filter(owner_identity=owner_identity, is_active=True)
    if source_record is not None:
        claims = claims.filter(source_record=source_record)
    return claims.update(is_active=False, released_at=timezone.now())
