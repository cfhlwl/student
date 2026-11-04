from __future__ import annotations

import subprocess
import sys
import time

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections

from collector.maintenance import MaintenanceError, collection_maintenance_gate, collection_write_gate
from collector.models import (
    CollectionActivity,
    CollectionEntry,
    CollectionProject,
    CollectionRecord,
    CollectionRun,
    ProjectConfigVersion,
    RecordIdentity,
    RecordUniqueValue,
    Submission,
    TemplateDefinition,
    TemplateVersion,
)
from collector.v2_contracts import (
    UniqueClaimConflict,
    build_export_snapshot,
    canonicalize_scope_key,
    canonicalize_unique_value,
    claim_unique_value,
    stable_json_sha256,
    validate_template_schema,
)


SCHEMA = {
    "fields": [
        {"key": "name", "label": "姓名", "type": "text", "required": True, "unique": True, "order": 0},
        {"key": "photo", "label": "照片", "type": "image_key", "order": 1},
    ]
}


@pytest.fixture
def project_graph(db):
    project = CollectionProject.objects.create(slug="demo", name="演示项目")
    template = TemplateDefinition.objects.create(project=project, key="people", name="人员模板")
    template_version = TemplateVersion.objects.create(
        template=template,
        version=1,
        state="published",
        schema_json=SCHEMA,
        schema_sha256=stable_json_sha256(SCHEMA),
        field_export_json={"columns": ["name", "photo"]},
    )
    config = ProjectConfigVersion.objects.create(
        project=project,
        version=1,
        state="published",
        settings_json={"brand": "demo"},
        export_policy_json={"layout": ["scope"], "workbook_names": {"detail": "明细"}},
        settings_sha256=stable_json_sha256({"brand": "demo"}),
    )
    project.active_config_version = config
    project.save(update_fields=["active_config_version", "updated_at"])
    entry_a = CollectionEntry.objects.create(
        project=project,
        slug="main",
        name="主入口",
        published_template_version=template_version,
        is_default=True,
    )
    entry_b = CollectionEntry.objects.create(
        project=project,
        slug="staff",
        name="员工入口",
        published_template_version=template_version,
    )
    return project, template_version, config, entry_a, entry_b


@pytest.mark.django_db
def test_v1_tables_and_v2_multi_entry_graph_are_isolated(project_graph):
    project, template_version, config, entry_a, entry_b = project_graph
    assert Submission._meta.db_table == "collector_submission"
    assert {entry_a.slug, entry_b.slug} == {"main", "staff"}
    assert project.active_config_version_id == config.pk

    activity = CollectionActivity.objects.create(project=project, entry=entry_a, name="2026", status="open")
    run = CollectionRun.objects.create(
        activity=activity,
        entry=entry_a,
        template_version=template_version,
        config_version=config,
        access_token_hash="a" * 64,
        template_schema_sha256=template_version.schema_sha256,
        config_sha256=config.settings_sha256,
        export_snapshot_json=build_export_snapshot(template_version, config),
    )
    assert run.entry_id == entry_a.pk
    assert run.export_snapshot_json["template"]["version"] == 1
    assert run.export_snapshot_json["config"]["version"] == 1


@pytest.mark.django_db
def test_published_entry_and_project_scope_validation(project_graph):
    project, template_version, config, entry_a, _ = project_graph
    draft = TemplateVersion.objects.create(
        template=template_version.template,
        version=2,
        state="draft",
        schema_json=SCHEMA,
        schema_sha256=stable_json_sha256(SCHEMA),
    )
    entry_a.published_template_version = draft
    with pytest.raises(ValidationError):
        entry_a.full_clean()


@pytest.mark.django_db
def test_schema_and_canonicalization_contract():
    assert validate_template_schema(SCHEMA) == SCHEMA
    assert canonicalize_unique_value("  Ａ  B  ") == "a b"
    assert canonicalize_unique_value("  Ａ  B  ", case_sensitive=True) == "A B"
    assert canonicalize_scope_key({"year": "2026", "class": " 1 "}, ["year", "class"]) == "4:2026|1:1"
    with pytest.raises(ValidationError):
        validate_template_schema({"fields": [{"key": "Bad", "label": "坏", "type": "text"}]})


@pytest.mark.django_db(transaction=True)
def test_global_claim_is_cross_template_and_released_history_is_retained(project_graph):
    project, template_version, _, _, _ = project_graph
    identity_a = RecordIdentity.objects.create(project=project, entity_type="employee", stable_key="a")
    identity_b = RecordIdentity.objects.create(project=project, entity_type="employee", stable_key="b")
    first = claim_unique_value(
        project=project,
        field_key="employee_no",
        namespace_kind="project",
        namespace_key="*",
        value=" E-001 ",
        owner_identity=identity_a,
    )
    assert first.canonical_value == "e-001"
    with pytest.raises(UniqueClaimConflict):
        claim_unique_value(
            project=project,
            field_key="employee_no",
            namespace_kind="project",
            namespace_key="*",
            value="e-001",
            owner_identity=identity_b,
        )
    released = RecordUniqueValue.objects.filter(pk=first.pk).update(is_active=False)
    assert released == 1
    second = claim_unique_value(
        project=project,
        field_key="employee_no",
        namespace_kind="project",
        namespace_key="*",
        value="E-001",
        owner_identity=identity_b,
    )
    assert second.pk != first.pk
    assert RecordUniqueValue.objects.filter(project=project, canonical_value="e-001").count() == 2
    template_claim_a = claim_unique_value(
        project=project,
        field_key="local_no",
        namespace_kind="template",
        namespace_key="template:people:v1",
        value="001",
        owner_identity=identity_a,
    )
    template_claim_b = claim_unique_value(
        project=project,
        field_key="local_no",
        namespace_kind="template",
        namespace_key="template:people:v2",
        value="001",
        owner_identity=identity_b,
    )
    assert template_claim_a.pk != template_claim_b.pk


@pytest.mark.django_db(transaction=True)
def test_claim_conflict_survives_two_threads_without_http500(project_graph):
    project, _, _, _, _ = project_graph
    identity_a = RecordIdentity.objects.create(project=project, entity_type="asset", stable_key="a")
    identity_b = RecordIdentity.objects.create(project=project, entity_type="asset", stable_key="b")
    barrier = __import__("threading").Barrier(2)
    results = []

    def worker(identity_id):
        close_old_connections()
        identity = RecordIdentity.objects.get(pk=identity_id)
        barrier.wait()
        try:
            claim_unique_value(
                project=CollectionProject.objects.get(pk=project.pk),
                field_key="asset_no",
                namespace_kind="project",
                namespace_key="*",
                value="asset-1",
                owner_identity=identity,
            )
        except UniqueClaimConflict:
            results.append("conflict")
        except Exception as exc:  # explicit evidence: no lock/500 escapes
            results.append(type(exc).__name__)
        else:
            results.append("success")
        finally:
            close_old_connections()

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(worker, [identity_a.pk, identity_b.pk]))
    assert sorted(results) == ["conflict", "success"]
    assert RecordUniqueValue.objects.filter(project=project, field_key="asset_no", is_active=True).count() == 1


@pytest.mark.django_db(transaction=True)
def test_maintenance_gate_is_shared_across_processes(tmp_path, settings):
    lock = tmp_path / "maintenance.lock"
    state = tmp_path / "maintenance.json"
    settings.COLLECTION_MAINTENANCE_LOCK_PATH = lock
    settings.COLLECTION_MAINTENANCE_STATE_PATH = state
    settings.COLLECTION_MAINTENANCE_LOCK_TIMEOUT_SECONDS = 1
    state.write_text('{"state":"open"}', encoding="utf-8")
    script = (
        "import portalocker,time,sys\n"
        "p=sys.argv[1]\n"
        "with portalocker.Lock(p, mode='a+b', flags=portalocker.LOCK_EX | portalocker.LOCK_NB, timeout=2):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(1.5)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "locked"
    started = time.monotonic()
    with pytest.raises(MaintenanceError):
        with collection_write_gate():
            pass
    assert time.monotonic() - started >= 0.8
    process.wait(timeout=5)
    with collection_maintenance_gate() as previous:
        assert previous == "open"

