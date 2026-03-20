# Testing

django-rls-tenants provides test helpers for setting up RLS context and verifying
that RLS policies are correctly applied.

## Context Managers

### rls_bypass

Enables admin bypass for the duration of the block. Useful for test setup and
teardown where you need to create/read data across tenants:

```python
from django_rls_tenants.tenants.testing import rls_bypass

def test_order_creation():
    with rls_bypass():
        # Create test data visible to all tenants
        tenant = Tenant.objects.create(name="Acme")
        Order.objects.create(tenant=tenant, title="Test", amount=100)

        # Verify data exists
        assert Order.objects.count() == 1
```

**Parameters:**

| Parameter | Type  | Default     | Description |
|-----------|-------|-------------|-------------|
| `using`   | `str` | `"default"` | Database alias. |

### rls_as_tenant

Scopes queries to a specific tenant. Useful for testing tenant isolation:

```python
from django_rls_tenants.tenants.testing import rls_as_tenant, rls_bypass

def test_tenant_isolation():
    with rls_bypass():
        t1 = Tenant.objects.create(name="Tenant 1")
        t2 = Tenant.objects.create(name="Tenant 2")
        Order.objects.create(tenant=t1, title="T1 Order", amount=100)
        Order.objects.create(tenant=t2, title="T2 Order", amount=200)

    with rls_as_tenant(tenant_id=t1.pk):
        orders = list(Order.objects.all())
        assert len(orders) == 1
        assert orders[0].title == "T1 Order"

    with rls_as_tenant(tenant_id=t2.pk):
        orders = list(Order.objects.all())
        assert len(orders) == 1
        assert orders[0].title == "T2 Order"
```

**Parameters:**

| Parameter   | Type        | Default     | Description |
|-------------|-------------|-------------|-------------|
| `tenant_id` | `int \| str` | *(required)* | Tenant PK to scope to. |
| `using`     | `str`       | `"default"` | Database alias. |

!!! info "Auto-scoping is active"
    Inside an `rls_as_tenant` block, `RLSManager.get_queryset()` automatically adds
    `WHERE tenant_id = X` to all queries. This means `Order.objects.all()` returns
    only the scoped tenant's rows at both ORM and RLS levels.

## Assertion Functions

### assert_rls_enabled

Verifies that RLS is enabled and forced on a table:

```python
from django_rls_tenants.tenants.testing import assert_rls_enabled

def test_rls_enabled_on_orders():
    assert_rls_enabled("myapp_order")
```

Raises `AssertionError` if:

- The table does not exist.
- RLS is not enabled (`relrowsecurity` is `False`).
- RLS is not forced (`relforcerowsecurity` is `False`).

### assert_rls_policy_exists

Verifies that an RLS policy exists on a table:

```python
from django_rls_tenants.tenants.testing import assert_rls_policy_exists

def test_isolation_policy_exists():
    assert_rls_policy_exists("myapp_order")

    # Custom policy name:
    assert_rls_policy_exists("myapp_order", policy_name="myapp_order_tenant_isolation_policy")
```

**Default policy name:** `"{table_name}_tenant_isolation_policy"`

### assert_rls_blocks_without_context

Verifies the fail-closed behavior -- queries without RLS context return zero rows:

```python
from django_rls_tenants.tenants.testing import assert_rls_blocks_without_context, rls_bypass

def test_fail_closed():
    with rls_bypass():
        # Must have at least one row for the assertion to be meaningful
        Order.objects.create(tenant=tenant, title="Test", amount=100)

    # Without any RLS context, zero rows should be returned
    assert_rls_blocks_without_context(Order)
```

This function:

1. Enters `admin_context()` to verify the table has at least one row (prevents
   vacuous passes on empty tables).
2. Queries without any GUC context and asserts zero rows are returned.

Raises `AssertionError` if:

- Rows are returned (RLS is not enforcing isolation).
- The table is empty (would pass vacuously).

## pytest Integration

### Using Fixtures

Create reusable fixtures for test data setup:

```python title="conftest.py"
import pytest
from django_rls_tenants.tenants.testing import rls_bypass, rls_as_tenant


@pytest.fixture
def tenant_a():
    with rls_bypass():
        return Tenant.objects.create(name="Tenant A")


@pytest.fixture
def tenant_b():
    with rls_bypass():
        return Tenant.objects.create(name="Tenant B")


@pytest.fixture
def sample_orders(tenant_a, tenant_b):
    with rls_bypass():
        Order.objects.create(tenant=tenant_a, title="A1", amount=100)
        Order.objects.create(tenant=tenant_a, title="A2", amount=200)
        Order.objects.create(tenant=tenant_b, title="B1", amount=300)
```

### Marking Tests

Use `pytest.mark.django_db` for tests that need database access:

```python
import pytest

pytestmark = pytest.mark.django_db


def test_tenant_sees_own_orders(sample_orders, tenant_a):
    with rls_as_tenant(tenant_id=tenant_a.pk):
        assert Order.objects.count() == 2


def test_no_context_sees_nothing(sample_orders):
    assert_rls_blocks_without_context(Order)
```

For tests that require transaction isolation:

```python
@pytest.mark.django_db(transaction=True)
def test_rls_with_transactions(tenant_a):
    # Tests that need real transaction boundaries
    ...
```

## Strict Mode in Tests

When `STRICT_MODE=True`, any query on an RLS-protected model without an active
context raises `NoTenantContextError`. This affects test setup code that queries
models outside a context.

Use `rls_bypass()` (which wraps `admin_context()`) or `rls_as_tenant()` in
fixture and setup code:

```python
@pytest.fixture
def sample_data():
    # rls_bypass establishes an active context -- passes strict mode check
    with rls_bypass():
        tenant = Tenant.objects.create(name="Test")
        Order.objects.create(tenant=tenant, title="Order 1", amount=100)
    return tenant
```

To explicitly test that strict mode raises, use `pytest.raises`:

```python
from django_rls_tenants import NoTenantContextError

@override_settings(RLS_TENANTS={**RLS_SETTINGS, "STRICT_MODE": True})
def test_strict_mode_raises_without_context():
    with pytest.raises(NoTenantContextError, match="strict mode"):
        Order.objects.count()
```

## Multi-Database Testing

All test helpers accept a `using` parameter:

```python
with rls_bypass(using="replica"):
    data = Order.objects.using("replica").all()

assert_rls_enabled("myapp_order", using="replica")
```

## Example: Full Test Suite

```python title="tests/test_orders.py"
import pytest
from django_rls_tenants.tenants.testing import (
    assert_rls_blocks_without_context,
    assert_rls_enabled,
    assert_rls_policy_exists,
    rls_as_tenant,
    rls_bypass,
)

pytestmark = pytest.mark.django_db


class TestOrderRLS:
    def test_rls_enabled(self):
        assert_rls_enabled("myapp_order")

    def test_policy_exists(self):
        assert_rls_policy_exists("myapp_order")

    def test_fail_closed(self, sample_orders):
        assert_rls_blocks_without_context(Order)

    def test_tenant_isolation(self, tenant_a, tenant_b, sample_orders):
        with rls_as_tenant(tenant_id=tenant_a.pk):
            a_orders = list(Order.objects.values_list("title", flat=True))
            assert sorted(a_orders) == ["A1", "A2"]

        with rls_as_tenant(tenant_id=tenant_b.pk):
            b_orders = list(Order.objects.values_list("title", flat=True))
            assert b_orders == ["B1"]

    def test_admin_sees_all(self, sample_orders):
        with rls_bypass():
            assert Order.objects.count() == 3
```
