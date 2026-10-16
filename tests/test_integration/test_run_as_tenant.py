"""Tests for the run_as_tenant management command.

The inner command is ``rls_probe`` (``tests/test_app/management/commands``): it
reports the tenant ``ContextVar``, the GUCs and a raw ``COUNT(*)`` of
``test_order``, so with ``enforce_rls`` the count proves the policy scoped it.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command, get_commands, load_command_class
from django.core.management.base import CommandError
from django.test import override_settings

from django_rls_tenants.tenants.state import get_current_tenant_id

pytestmark = pytest.mark.django_db


def _run(*args, **options):
    """Run ``run_as_tenant`` and return the inner command's JSON line."""
    out = StringIO()
    call_command("run_as_tenant", *map(str, args), stdout=out, **options)
    return json.loads(out.getvalue())


class TestTenantContext:
    """The inner command runs inside the resolved tenant's context."""

    def test_by_pk(self, sample_orders, tenant_a):
        probe = _run("--tenant", tenant_a.pk, "rls_probe")
        assert probe["tenant"] == tenant_a.pk
        assert probe["guc"] == str(tenant_a.pk)
        assert probe["is_admin"] == "false"
        assert probe["orders"] == 2

    def test_by_lookup_field(self, sample_orders, tenant_b):
        probe = _run("--tenant", "Tenant B", "--lookup", "name", "rls_probe")
        assert probe["tenant"] == tenant_b.pk
        assert probe["orders"] == 1

    def test_admin(self, sample_orders):
        probe = _run("--admin", "rls_probe")
        assert probe["tenant"] is None
        assert probe["is_admin"] == "true"
        assert probe["orders"] == 3

    def test_context_ends_with_the_command(self, tenant_a):
        _run("--tenant", tenant_a.pk, "rls_probe")
        assert get_current_tenant_id() is None

    def test_inner_command_error_propagates(self, tenant_a):
        with pytest.raises(CommandError, match="probe failed"):
            _run("--tenant", tenant_a.pk, "rls_probe", "--fail-for", tenant_a.pk)
        assert get_current_tenant_id() is None

    def test_verbose_names_the_tenant_on_stderr(self, tenant_a):
        err = StringIO()
        _run("--tenant", tenant_a.pk, "-v", "2", "rls_probe", stderr=err)
        assert f"Running rls_probe in tenant Tenant A (pk={tenant_a.pk})" in err.getvalue()


class TestArguments:
    """Everything after the command's name belongs to it."""

    def test_arguments_and_options_are_passed_on(self, tenant_a):
        probe = _run("--tenant", tenant_a.pk, "rls_echo", "one", "two", "--flag", "x")
        assert probe == {"words": ["one", "two"], "flag": "x", "tenant": tenant_a.pk}

    def test_leading_double_dash_is_dropped(self, tenant_a):
        probe = _run("--tenant", tenant_a.pk, "rls_echo", "--", "--flag", "x")
        assert probe["flag"] == "x"

    def test_options_after_the_name_are_not_ours(self, tenant_a):
        # --tenant after the name goes to rls_echo, which has no such option.
        with pytest.raises(CommandError, match="unrecognized arguments: --tenant"):
            _run("--admin", "rls_echo", "--tenant", tenant_a.pk)

    def test_a_loaded_command_object(self, tenant_a, monkeypatch):
        # get_commands() may hold a command object instead of an app name, as
        # call_command() allows; the --database check must load it the same way.
        command = load_command_class("tests.test_app", "rls_probe")
        monkeypatch.setitem(get_commands(), "rls_loaded", command)
        probe = _run("--database", "default", "--tenant", tenant_a.pk, "rls_loaded")
        assert (probe["tenant"], probe["database"]) == (tenant_a.pk, "default")

    def test_tenant_or_admin_required(self):
        with pytest.raises(CommandError, match="Pass --tenant <value>"):
            _run("rls_probe")

    def test_tenant_and_admin_are_exclusive(self, tenant_a):
        with pytest.raises(CommandError, match="not allowed with argument"):
            _run("--tenant", tenant_a.pk, "--admin", "rls_probe")

    def test_unknown_command(self, tenant_a):
        with pytest.raises(CommandError, match="Unknown command: 'no_such_command'"):
            _run("--tenant", tenant_a.pk, "no_such_command")


class TestTenantResolution:
    """Errors resolving ``--tenant`` are reported, not raised as tracebacks."""

    def test_unknown_tenant(self, tenant_a):
        with pytest.raises(CommandError, match="No Tenant with pk='0'"):
            _run("--tenant", 0, "rls_probe")

    def test_ambiguous_lookup(self, tenant_a, tenant_b):
        with pytest.raises(CommandError, match="More than one Tenant"):
            _run("--tenant", "Tenant", "--lookup", "name__startswith", "rls_probe")

    def test_value_of_the_wrong_type(self, tenant_a):
        with pytest.raises(CommandError, match="Cannot look up a Tenant by pk='abc'"):
            _run("--tenant", "abc", "rls_probe")

    def test_unknown_lookup_field(self, tenant_a):
        with pytest.raises(CommandError, match="Cannot look up a Tenant by slug="):
            _run("--tenant", "acme", "--lookup", "slug", "rls_probe")


class TestDatabase:
    """``--database`` picks the alias and is passed on to commands that declare it."""

    def test_passed_on_to_a_command_with_the_option(self, replica, tenant_a):
        probe = _run("--database", replica, "--tenant", tenant_a.pk, "rls_probe")
        assert probe["database"] == replica
        assert probe["guc"] == str(tenant_a.pk)

    def test_explicit_inner_option_wins(self, replica, tenant_a):
        probe = _run(
            "--database", replica, "--tenant", tenant_a.pk, "rls_probe", "--database", "default"
        )
        assert probe["database"] == "default"

    def test_not_passed_to_a_command_without_it(self, replica, tenant_a):
        probe = _run("--database", replica, "--tenant", tenant_a.pk, "rls_echo")
        assert probe["tenant"] == tenant_a.pk

    def test_not_passed_on_when_not_given(self, tenant_a):
        probe = _run("--tenant", tenant_a.pk, "rls_probe")
        assert probe["database"] == "default"

    def test_unknown_alias(self, tenant_a):
        with pytest.raises(CommandError, match="Unknown database alias 'nope'"):
            _run("--database", "nope", "--tenant", tenant_a.pk, "rls_probe")

    def test_local_mode_needs_a_configured_alias(self, replica, tenant_a, settings):
        rls = {**settings.RLS_TENANTS, "USE_LOCAL_SET": True}
        with (
            override_settings(RLS_TENANTS=rls),
            pytest.raises(CommandError, match="not in RLS_TENANTS\\['DATABASES'\\]"),
        ):
            _run("--database", replica, "--tenant", tenant_a.pk, "rls_probe")
