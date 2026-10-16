"""Shared pytest fixtures for the test suite.

``rls_role``, ``enforce_rls`` and ``enforce_rls_on_other_threads`` come from the
library's own pytest plugin (``django_rls_tenants.pytest_plugin``), which the
``pytest11`` entry point loads, so the suite runs on the fixtures it ships.
"""

from __future__ import annotations

import contextlib
import os
from decimal import Decimal

import pytest
from django.db import DEFAULT_DB_ALIAS, connections

from django_rls_tenants.pytest_plugin import _bypasses_rls, _switch_role
from django_rls_tenants.rls.guc import clear_guc
from django_rls_tenants.tenants.context import admin_context
from django_rls_tenants.tenants.middleware import _clear_gucs_set_flag
from django_rls_tenants.tenants.state import (
    _clear_context_gucs,
    set_current_tenant_id,
    set_rls_context_active,
)
from tests.test_app.models import (
    Document,
    NoteRevision,
    Order,
    OrderItem,
    OrderNote,
    OrderNoteAttachment,
    Project,
    ProtectedUser,
    SelfRefModel,
    Tag,
    Tenant,
    TenantUser,
)

_POOLED_ALIAS = "pooled"

# Zero-skipped-tests rule (#110). A skipped RLS test is indistinguishable from
# a CI leg whose database role never materialised, so every PostgreSQL CI leg
# sets this variable and the session fails if anything was skipped. Local runs
# leave it unset and skips behave as usual.
_FAIL_ON_SKIPPED_ENV = "RLS_FAIL_ON_SKIPPED"

# GUC variable names that may be set during tests.
_GUC_NAMES_TO_CLEAR = (
    "rls.is_admin",
    "rls.current_tenant",
    "rls.is_login_request",
    "rls.is_preauth_request",
)


def pytest_configure(config):
    """Turn every Django deprecation warning into a test error.

    Classes are discovered at runtime (``RemovedInDjango2028Warning`` today) so the
    year-based rename in Django 2028 cannot break the hook. The newest Django leg then
    fails on the day Django deprecates something this library calls (#114).
    """
    from django.utils import deprecation  # noqa: PLC0415  -- keep conftest import-light

    for name, obj in vars(deprecation).items():
        if name.startswith("RemovedIn") and isinstance(obj, type) and issubclass(obj, Warning):
            config.addinivalue_line("filterwarnings", f"error::django.utils.deprecation.{name}")


def pytest_collection_modifyitems(config, items):
    """Deselect ``psycopg3_only`` tests unless Django runs on psycopg 3.

    Django's connection pool needs psycopg 3 and ``psycopg_pool``, and Django
    switches to psycopg 3 whenever it is importable, so only the psycopg 3 CI
    leg can run these tests. Deselected tests are not skipped, so the
    zero-skipped rule still holds; that leg's driver check proves it really
    runs psycopg 3, and a missing ``psycopg_pool`` there fails the tests.
    """
    from django.db.backends.postgresql.psycopg_any import (  # noqa: PLC0415  -- keep conftest import-light
        is_psycopg3,
    )

    if is_psycopg3:
        return
    deselected = [item for item in items if item.get_closest_marker("psycopg3_only")]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if not item.get_closest_marker("psycopg3_only")]


def pytest_sessionfinish(session, exitstatus):
    """Fail the session on any skipped test when ``RLS_FAIL_ON_SKIPPED=1``.

    Deselected tests (``-m "not superuser_only"``) and ``xfail`` outcomes are
    not skips and do not trip the rule. Runs inside the terminal reporter's
    own ``sessionfinish`` wrapper, so the notice lands above the summary line.
    """
    if os.environ.get(_FAIL_ON_SKIPPED_ENV) != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    skipped = reporter.stats.get("skipped", [])
    if not skipped:
        return
    reporter.write_sep(
        "=",
        f"{len(skipped)} skipped test(s) but {_FAIL_ON_SKIPPED_ENV}=1: a skipped RLS test "
        "proves nothing, so the session fails",
        red=True,
        bold=True,
    )
    for report in skipped:
        longrepr = getattr(report, "longrepr", None)
        reason = longrepr[-1] if isinstance(longrepr, tuple) else ""
        reporter.write_line(f"  {report.nodeid}: {reason}", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _clear_gucs_after_test(db):
    """Clear known GUC variables and auto-scope state after each test.

    Prevents GUC and ContextVar state from leaking between tests when
    a test fails before reaching manual cleanup. Runs automatically
    for all tests.
    """
    yield
    try:
        set_current_tenant_id(None)
        set_rls_context_active(False)
        # A test that calls process_request() alone leaves the request's GUC
        # values here; the affinity guard would apply them in later tests.
        _clear_context_gucs()
        # A middleware test can leave this set; the connection_created handler
        # would then put GUCs on the next new connection, outside any test
        # transaction.
        _clear_gucs_set_flag()
    finally:
        for name in _GUC_NAMES_TO_CLEAR:
            with contextlib.suppress(Exception):
                clear_guc(name)


@pytest.fixture
def replica(rls_role, transactional_db):
    """A second connection to the test database, registered as the ``replica`` alias.

    It is a separate session, so it sees committed rows only (hence
    ``transactional_db``), and it runs as a role RLS applies to, like
    ``enforce_rls`` does for ``default``. Only ``connections`` knows the
    alias, not ``settings.DATABASES``, so the teardown flush never touches it.
    """
    default = connections[DEFAULT_DB_ALIAS]
    conn = type(default)(default.settings_dict, alias="replica")
    connections["replica"] = conn
    try:
        _switch_role(conn, rls_role)
        yield "replica"
    finally:
        del connections["replica"]
        conn.close()


@pytest.fixture
def pooled(request, rls_role, transactional_db):
    """A ``pooled`` alias on the test database, served by Django's pool of one connection.

    Every checkout gets the same physical connection, so a test sees what the
    previous borrower left on it. Like ``replica`` it is a separate session
    (committed rows only), registered on this thread only, and it runs as a
    role RLS applies to, set at connection startup through libpq ``options``.
    (Django 6.1's ``assume_role`` cannot connect through a pool: it composes
    ``SET ROLE`` with a cursor of the same alias, which waits on the pool that
    is still configuring the connection.) Django's test client never returns
    connections at request boundaries (it disconnects ``close_old_connections``),
    so tests close the alias where a server would. Indirect parametrization
    adds pool options.

    Needs psycopg 3 and ``psycopg_pool``: request it from ``psycopg3_only`` tests.
    """
    default = connections[DEFAULT_DB_ALIAS]
    options = {
        **default.settings_dict["OPTIONS"],
        "pool": {"min_size": 1, "max_size": 1, "timeout": 10, **getattr(request, "param", {})},
    }
    # session_user: enforce_rls may already have switched current_user.
    if _bypasses_rls(default, role="session_user"):
        options["options"] = f"-c role={rls_role}"
    conn = type(default)({**default.settings_dict, "OPTIONS": options}, alias=_POOLED_ALIAS)
    connections[_POOLED_ALIAS] = conn
    try:
        yield _POOLED_ALIAS
    finally:
        del connections[_POOLED_ALIAS]
        try:
            conn.close()
        finally:
            conn.close_pool()


@pytest.fixture
def tenant_a(db):
    """Tenant A."""
    return Tenant.objects.create(name="Tenant A")


@pytest.fixture
def tenant_b(db):
    """Tenant B."""
    return Tenant.objects.create(name="Tenant B")


@pytest.fixture
def admin_user(db):
    """Admin user (no tenant, is_admin=True)."""
    return TenantUser.objects.create(
        username="admin",
        tenant=None,
        is_admin=True,
    )


@pytest.fixture
def tenant_a_user(db, tenant_a):
    """Regular user belonging to Tenant A."""
    return TenantUser.objects.create(
        username="user_a",
        tenant=tenant_a,
        is_admin=False,
    )


@pytest.fixture
def tenant_b_user(db, tenant_b):
    """Regular user belonging to Tenant B."""
    return TenantUser.objects.create(
        username="user_b",
        tenant=tenant_b,
        is_admin=False,
    )


@pytest.fixture
def sample_orders(db, tenant_a, tenant_b):
    """Create sample orders for both tenants. Requires admin context for RLS."""
    with admin_context():
        order_a1 = Order.objects.create(
            product="Widget A1",
            amount=Decimal("10.00"),
            tenant=tenant_a,
        )
        order_a2 = Order.objects.create(
            product="Widget A2",
            amount=Decimal("20.00"),
            tenant=tenant_a,
        )
        order_b1 = Order.objects.create(
            product="Gadget B1",
            amount=Decimal("30.00"),
            tenant=tenant_b,
        )
    return {"a1": order_a1, "a2": order_a2, "b1": order_b1}


@pytest.fixture
def sample_order_items(db, sample_orders):
    """Create sample order items linked to orders. Requires admin context for RLS."""
    with admin_context():
        item_a1 = OrderItem.objects.create(
            order=sample_orders["a1"],
            description="Part A1-1",
            tenant=sample_orders["a1"].tenant,
        )
        item_a2 = OrderItem.objects.create(
            order=sample_orders["a2"],
            description="Part A2-1",
            tenant=sample_orders["a2"].tenant,
        )
        item_b1 = OrderItem.objects.create(
            order=sample_orders["b1"],
            description="Part B1-1",
            tenant=sample_orders["b1"].tenant,
        )
    return {"a1": item_a1, "a2": item_a2, "b1": item_b1}


@pytest.fixture
def sample_order_notes(db, sample_orders, tenant_a, tenant_b):
    """Create order notes with mix of linked and NULL order FKs.

    Tenant A gets one note linked to an order and one with no order (NULL FK).
    Tenant B gets one note linked to an order.
    """
    with admin_context():
        note_a_linked = OrderNote.objects.create(
            order=sample_orders["a1"],
            body="Linked to order A1",
            tenant=tenant_a,
        )
        note_a_orphan = OrderNote.objects.create(
            order=None,
            body="No order (orphan)",
            tenant=tenant_a,
        )
        note_b_linked = OrderNote.objects.create(
            order=sample_orders["b1"],
            body="Linked to order B1",
            tenant=tenant_b,
        )
    return {
        "a_linked": note_a_linked,
        "a_orphan": note_a_orphan,
        "b_linked": note_b_linked,
    }


@pytest.fixture
def sample_nested_rows(db, sample_order_items, sample_order_notes, tenant_a, tenant_b):
    """Rows two hops away from ``Order`` with NULLs at different hops (#79).

    Tenant A attachments (nullable first hop):

    - ``full``: note -> order A1 and item -> order A1 (whole chain non-NULL)
    - ``leaf_null``: note -> orphan note (``note.order`` is NULL), no item
    - ``orphan``: no note, no item (first hop NULL)

    Tenant A revisions (non-null first hop):

    - ``full``: note -> order A1, item -> order A1
    - ``leaf_null``: note -> orphan note (``note.order`` is NULL), item -> order A2

    Tenant B gets one fully linked row of each kind.
    """
    with admin_context():
        att_full = OrderNoteAttachment.objects.create(
            note=sample_order_notes["a_linked"],
            item=sample_order_items["a1"],
            filename="full.txt",
            tenant=tenant_a,
        )
        att_leaf_null = OrderNoteAttachment.objects.create(
            note=sample_order_notes["a_orphan"],
            item=None,
            filename="leaf-null.txt",
            tenant=tenant_a,
        )
        att_orphan = OrderNoteAttachment.objects.create(
            note=None,
            item=None,
            filename="orphan.txt",
            tenant=tenant_a,
        )
        att_b = OrderNoteAttachment.objects.create(
            note=sample_order_notes["b_linked"],
            item=sample_order_items["b1"],
            filename="b.txt",
            tenant=tenant_b,
        )
        rev_full = NoteRevision.objects.create(
            note=sample_order_notes["a_linked"],
            item=sample_order_items["a1"],
            summary="full",
            tenant=tenant_a,
        )
        rev_leaf_null = NoteRevision.objects.create(
            note=sample_order_notes["a_orphan"],
            item=sample_order_items["a2"],
            summary="leaf-null",
            tenant=tenant_a,
        )
        rev_b = NoteRevision.objects.create(
            note=sample_order_notes["b_linked"],
            item=sample_order_items["b1"],
            summary="b",
            tenant=tenant_b,
        )
    return {
        "att_full": att_full,
        "att_leaf_null": att_leaf_null,
        "att_orphan": att_orphan,
        "att_b": att_b,
        "rev_full": rev_full,
        "rev_leaf_null": rev_leaf_null,
        "rev_b": rev_b,
    }


@pytest.fixture
def sample_documents(db, tenant_a, tenant_b):
    """Create sample documents for both tenants. Requires admin context for RLS."""
    with admin_context():
        doc_a = Document.objects.create(title="Doc A", tenant=tenant_a)
        doc_b = Document.objects.create(title="Doc B", tenant=tenant_b)
    return {"a": doc_a, "b": doc_b}


@pytest.fixture
def sample_protected_users(db, tenant_a, tenant_b):
    """Create sample protected users for both tenants."""
    with admin_context():
        pu_a = ProtectedUser.objects.create(
            email="alice@a.com",
            tenant=tenant_a,
        )
        pu_b = ProtectedUser.objects.create(
            email="bob@b.com",
            tenant=tenant_b,
        )
    return {"a": pu_a, "b": pu_b}


# ---------------------------------------------------------------------------
# M2M fixtures (Phase 3 - Issue #11)
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_tags(db):
    """Create sample tags (non-RLS model)."""
    tag_x = Tag.objects.create(name="Tag X")
    tag_y = Tag.objects.create(name="Tag Y")
    return {"x": tag_x, "y": tag_y}


@pytest.fixture
def sample_projects(db, tenant_a, tenant_b, sample_protected_users, sample_tags):
    """Create projects with M2M relationships for both tenants."""
    with admin_context():
        proj_a = Project.objects.create(name="Project A", tenant=tenant_a)
        proj_b = Project.objects.create(name="Project B", tenant=tenant_b)

        # M2M: members (both sides RLS-protected)
        proj_a.members.add(sample_protected_users["a"])
        proj_b.members.add(sample_protected_users["b"])

        # M2M: tags (one side RLS-protected)
        proj_a.tags.add(sample_tags["x"])
        proj_b.tags.add(sample_tags["y"])

    return {"a": proj_a, "b": proj_b}


@pytest.fixture
def sample_selfref(db, tenant_a, tenant_b):
    """Create self-referential M2M test data."""
    with admin_context():
        sr_a1 = SelfRefModel.objects.create(name="SR A1", tenant=tenant_a)
        sr_a2 = SelfRefModel.objects.create(name="SR A2", tenant=tenant_a)
        sr_b1 = SelfRefModel.objects.create(name="SR B1", tenant=tenant_b)

        sr_a1.friends.add(sr_a2)
        sr_b1.friends.add(sr_b1)  # self-friend

    return {"a1": sr_a1, "a2": sr_a2, "b1": sr_b1}
