"""Tests for the run_for_all_tenants management command.

The inner command is ``rls_probe`` (see ``test_run_as_tenant.py``). Workers of
``--parallel`` are separate processes on their own connections, logged in as
the test database's login role: on a superuser CI leg their raw counts are not
filtered, so those tests check the context (the ``ContextVar`` and the GUC)
instead of the count.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tests.test_app.models import Tenant

pytestmark = pytest.mark.django_db


def _run(*args, **options):
    """Run ``run_for_all_tenants`` and return ``(stdout, stderr)``."""
    out, err = StringIO(), StringIO()
    call_command("run_for_all_tenants", *map(str, args), stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


def _json_lines(text):
    """Parse NDJSON output into ``{tenant pk: record}``."""
    return {record["tenant"]: record for record in map(json.loads, text.splitlines())}


def _probe(record):
    """Return the ``rls_probe`` line a tenant's record captured."""
    return json.loads(record["stdout"])


class TestSerial:
    """Without ``--parallel`` the tenants run one after another in this process."""

    def test_each_tenant_runs_in_its_context(self, sample_orders, tenant_a, tenant_b):
        out, _ = _run("rls_probe")
        assert f"== Tenant A (pk={tenant_a.pk}) ==" in out
        probes = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        assert [(p["tenant"], p["guc"], p["orders"]) for p in probes] == [
            (tenant_a.pk, str(tenant_a.pk), 2),
            (tenant_b.pk, str(tenant_b.pk), 1),
        ]
        assert f"Tenant B (pk={tenant_b.pk}): OK in " in out
        assert out.splitlines()[-1] == "rls_probe: 2 tenant(s) succeeded, 0 failed, 0 skipped."

    def test_json(self, sample_orders, tenant_a, tenant_b):
        out, _ = _run("--json", "rls_probe", "--warn", "careful")
        records = _json_lines(out)
        assert list(records) == [tenant_a.pk, tenant_b.pk]
        record = records[tenant_a.pk]
        assert record["label"] == "Tenant A"
        assert record["status"] == "ok"
        assert record["error"] is None
        assert record["stderr"] == "careful\n"
        assert isinstance(record["seconds"], float)
        assert _probe(record)["orders"] == 2
        assert _probe(records[tenant_b.pk])["orders"] == 1

    def test_quiet(self, tenant_a, tenant_b):
        out, _ = _run("-v", "0", "rls_echo")
        assert [json.loads(line)["tenant"] for line in out.splitlines()] == [
            tenant_a.pk,
            tenant_b.pk,
        ]

    def test_no_tenants(self):
        out, _ = _run("rls_probe")
        assert out == "No tenants matched; nothing to run.\n"

    def test_arguments_are_passed_on(self, tenant_a):
        out, _ = _run("--json", "rls_echo", "w", "--flag", "x")
        assert json.loads(_json_lines(out)[tenant_a.pk]["stdout"])["flag"] == "x"

    def test_database_is_passed_on(self, replica, tenant_a):
        out, _ = _run("--json", "--database", replica, "rls_probe")
        probe = _probe(_json_lines(out)[tenant_a.pk])
        assert probe["database"] == replica
        assert probe["guc"] == str(tenant_a.pk)


class TestFilter:
    """``--filter`` narrows the tenants with ``QuerySet.filter()`` lookups."""

    def test_exact(self, tenant_a, tenant_b):
        out, _ = _run("--json", "--filter", "name=Tenant B", "rls_echo")
        assert list(_json_lines(out)) == [tenant_b.pk]

    def test_lookups_and_repeated(self, tenant_a, tenant_b):
        Tenant.objects.create(name="Other")
        out, _ = _run(
            "--json",
            "--filter",
            "name__startswith=Tenant",
            "--filter",
            f"pk__gt={tenant_a.pk}",
            "rls_echo",
        )
        assert list(_json_lines(out)) == [tenant_b.pk]

    def test_malformed(self, tenant_a):
        with pytest.raises(CommandError, match="--filter expects field=value"):
            _run("--filter", "name", "rls_echo")

    def test_unknown_field(self, tenant_a):
        with pytest.raises(CommandError, match="Cannot filter Tenant by"):
            _run("--filter", "slug=acme", "rls_echo")


class TestFailures:
    """A failing tenant does not stop the others, but fails the command."""

    def test_failure_is_reported_and_fails_the_command(self, tenant_a, tenant_b):
        out = StringIO()
        with pytest.raises(CommandError) as excinfo:
            call_command(
                "run_for_all_tenants",
                "--json",
                "rls_probe",
                "--fail-for",
                str(tenant_a.pk),
                stdout=out,
            )
        assert str(excinfo.value) == (
            f"rls_probe failed for 1 of 2 tenant(s): Tenant A (pk={tenant_a.pk})"
        )
        records = _json_lines(out.getvalue())
        assert records[tenant_a.pk]["status"] == "failed"
        assert records[tenant_a.pk]["error"] == (
            f"CommandError: probe failed for tenant {tenant_a.pk}"
        )
        assert "Traceback" in records[tenant_a.pk]["traceback"]
        assert records[tenant_b.pk]["status"] == "ok"

    def test_human_output_and_traceback(self, tenant_a, tenant_b):
        err = StringIO()
        with pytest.raises(CommandError):
            call_command(
                "run_for_all_tenants",
                "--traceback",
                "rls_probe",
                "--fail-for",
                str(tenant_b.pk),
                stdout=StringIO(),
                stderr=err,
            )
        assert f"Tenant B (pk={tenant_b.pk}): FAILED in " in err.getvalue()
        assert "Traceback (most recent call last)" in err.getvalue()

    def test_fail_fast_skips_the_rest(self, tenant_a, tenant_b):
        out = StringIO()
        with pytest.raises(CommandError, match="failed for 1 of 2"):
            call_command(
                "run_for_all_tenants",
                "--json",
                "--fail-fast",
                "rls_probe",
                "--fail-for",
                str(tenant_a.pk),
                stdout=out,
            )
        records = _json_lines(out.getvalue())
        assert records[tenant_a.pk]["status"] == "failed"
        assert records[tenant_b.pk] == {
            "tenant": tenant_b.pk,
            "label": "Tenant B",
            "status": "skipped",
            "seconds": None,
            "error": None,
            "traceback": None,
            "stdout": None,
            "stderr": None,
        }

    def test_fail_fast_human_output(self, tenant_a, tenant_b):
        out = StringIO()
        with pytest.raises(CommandError):
            call_command(
                "run_for_all_tenants",
                "--fail-fast",
                "rls_probe",
                "--fail-for",
                str(tenant_a.pk),
                stdout=out,
                stderr=StringIO(),
            )
        assert f"Tenant B (pk={tenant_b.pk}): skipped" in out.getvalue()
        assert "rls_probe: 0 tenant(s) succeeded, 1 failed, 1 skipped." in out.getvalue()

    def test_non_zero_exit_is_a_failure(self, tenant_a):
        out = StringIO()
        with pytest.raises(CommandError):
            call_command("run_for_all_tenants", "--json", "rls_probe", "--exit", "3", stdout=out)
        assert _json_lines(out.getvalue())[tenant_a.pk]["error"] == "exited with status 3"

    def test_zero_exit_is_a_success(self, tenant_a):
        out, _ = _run("--json", "rls_probe", "--exit", "0")
        assert _json_lines(out)[tenant_a.pk]["status"] == "ok"

    def test_unknown_command(self, tenant_a):
        with pytest.raises(CommandError, match="Unknown command"):
            _run("no_such_command")

    def test_parallel_must_be_positive(self, tenant_a):
        with pytest.raises(CommandError, match="--parallel must be at least 1"):
            _run("--parallel", "0", "rls_probe")


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
class TestParallel:
    """``--parallel`` runs tenants in spawned worker processes (committed rows only)."""

    @pytest.fixture(autouse=True)
    def _third_tenant(self, transactional_db):
        Tenant.objects.create(name="Tenant C")

    def test_workers_run_each_tenant_in_its_context(self, tenant_a, tenant_b):
        out = StringIO()
        with pytest.raises(CommandError, match="failed for 1 of 3"):
            call_command(
                "run_for_all_tenants",
                "--parallel",
                "2",
                "--json",
                "rls_probe",
                "--fail-for",
                str(tenant_a.pk),
                stdout=out,
            )
        tenant_c = Tenant.objects.get(name="Tenant C")
        records = _json_lines(out.getvalue())
        assert set(records) == {tenant_a.pk, tenant_b.pk, tenant_c.pk}
        assert records[tenant_a.pk]["status"] == "failed"
        for tenant in (tenant_b, tenant_c):
            probe = _probe(records[tenant.pk])
            assert (probe["tenant"], probe["guc"]) == (tenant.pk, str(tenant.pk))

    def test_human_output(self, tenant_a, tenant_b):
        out, _ = _run("--parallel", "2", "rls_echo")
        lines = out.splitlines()
        probes = [json.loads(line) for line in lines if line.startswith("{")]
        assert sorted(p["tenant"] for p in probes) == sorted(
            Tenant.objects.values_list("pk", flat=True)
        )
        # Each worker's captured output follows its tenant's header.
        header = lines.index(f"== Tenant A (pk={tenant_a.pk}) ==")
        assert json.loads(lines[header + 1])["tenant"] == tenant_a.pk
        assert "rls_echo: 3 tenant(s) succeeded, 0 failed, 0 skipped." in out

    def test_a_dead_worker_fails_its_tenants(self, tenant_a, tenant_b):
        out = StringIO()
        with pytest.raises(CommandError, match="rls_probe failed for"):
            call_command(
                "run_for_all_tenants",
                "--parallel",
                "2",
                "--json",
                "rls_probe",
                "--crash-for",
                str(tenant_a.pk),
                stdout=out,
            )
        record = _json_lines(out.getvalue())[tenant_a.pk]
        assert record["status"] == "failed"
        assert record["error"].startswith("the worker process died: ")

    def test_needs_a_settings_module(self, tenant_a, settings):
        settings.SETTINGS_MODULE = None
        with pytest.raises(CommandError, match="--parallel needs a settings module"):
            _run("--parallel", "2", "rls_probe")
