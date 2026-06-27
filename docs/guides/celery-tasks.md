# Celery Tasks

Celery tasks run outside the request/response cycle, so `RLSTenantMiddleware`
never sets the RLS context for them. Each task must establish its own context
explicitly -- otherwise, queries against RLS-protected models return zero rows
(fail-closed), or raise `NoTenantContextError` when `STRICT_MODE` is enabled.

!!! note
    Native Celery integration (automatic context propagation without manual
    wiring) is planned for v1.3.0. This page documents the interim pattern.

## Core Pattern

Pass `tenant_id` as a task argument and wrap the task body in `tenant_context()`:

```python
from celery import shared_task
from django_rls_tenants import tenant_context


@shared_task
def process_orders(tenant_id: int) -> None:
    with tenant_context(tenant_id):
        for order in Order.objects.all():   # scoped to this tenant
            ...
```

!!! warning
    Pass the tenant **id** (an `int` or `str`), never a model instance. Celery
    serialises task arguments, and a serialised model instance is both wasteful
    and can go stale before the task executes.

!!! warning
    Every task that queries RLS-protected models must set a context. A task that
    forgets gets zero rows (fail-closed), or raises `NoTenantContextError` when
    `STRICT_MODE=True`.

## Enqueuing from a Request

The current tenant's ID is typically available as `request.user.rls_tenant_id`:

```python
# In a view:
process_orders.delay(request.user.rls_tenant_id)
```

## Cross-Tenant and Scheduled Tasks

For periodic (beat) tasks that operate across all tenants, use `admin_context()`
to read the tenant list, then re-enter `tenant_context()` for each tenant:

```python
from celery import shared_task
from django_rls_tenants import admin_context, tenant_context


@shared_task
def nightly_billing() -> None:
    with admin_context():
        tenants = Tenant.objects.all()
        for tenant in tenants:
            with tenant_context(tenant_id=tenant.pk):
                _process_orders(tenant)


def _process_orders(tenant: Tenant) -> None:
    orders = Order.objects.filter(status="pending")
    for order in orders:
        order.process()
```

!!! warning
    `admin_context()` bypasses tenant isolation entirely -- it sees every
    tenant's data. Keep its body minimal: use it only to fetch the tenant list,
    then scope each operation back down with `tenant_context()`. Never mix in
    user-supplied tenant filtering inside `admin_context()`.

## Reusable Decorator

To avoid repeating the `with tenant_context(tenant_id):` block in every task,
write a thin decorator that reads `tenant_id` from the first argument:

```python
import functools
from django_rls_tenants import tenant_context


def with_tenant_context(func):
    """Decorator that opens tenant_context(tenant_id) around the task body.

    Expects ``tenant_id`` as the first positional argument.
    """
    @functools.wraps(func)
    def wrapper(tenant_id, *args, **kwargs):
        with tenant_context(tenant_id):
            return func(tenant_id, *args, **kwargs)
    return wrapper
```

```python
from celery import shared_task


@shared_task
@with_tenant_context
def process_orders(tenant_id: int) -> None:
    for order in Order.objects.all():   # already scoped by decorator
        ...
```

!!! warning
    This decorator assumes `tenant_id` is the **first positional argument**, so it
    does not work with bound tasks (`@shared_task(bind=True)`) -- there Celery passes
    the task instance first, and `tenant_id` would receive `self`. For bound tasks,
    keep the explicit `with tenant_context(tenant_id):` block in the task body instead.

!!! tip
    In multi-database setups, pass `using="..."` to `tenant_context()` to scope
    queries on a specific database alias:

    ```python
    with tenant_context(tenant_id, using="replica"):
        orders = Order.objects.using("replica").all()
    ```

    See [Context Managers](context-managers.md#multi-database-support) for details.

!!! note
    See [Context Managers](context-managers.md) for the full `tenant_context()` and
    `admin_context()` API -- including nesting, debug logging, and strict mode
    interaction. Native Celery integration is on the v1.3.0 roadmap.
