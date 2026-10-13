# Context Managers

Context managers provide explicit control over RLS context in code that runs outside
the request/response cycle -- management commands, Celery tasks, tests, scripts, and
service-layer functions.

## tenant_context

Scopes all database queries within the block to a specific tenant:

```python
from django_rls_tenants import tenant_context

with tenant_context(tenant_id=42):
    # All queries see only tenant 42's data
    orders = Order.objects.all()
    invoices = Invoice.objects.all()
```

**Parameters:**

| Parameter   | Type        | Default     | Description |
|-------------|-------------|-------------|-------------|
| `tenant_id` | `int \| str` | *(required)* | The tenant PK to scope to. |
| `using`     | `str`       | `"default"` | Database alias. |

**Behavior:**

- Sets `rls.is_admin = 'false'` and `rls.current_tenant = str(tenant_id)`.
- Sets the internal `ContextVar` state so `RLSManager.get_queryset()` automatically
  adds `WHERE tenant_id = X` to all queries (auto-scoping).
- Saves and restores previous GUC values and state on exit (supports nesting).
- Raises `NoTenantContextError` if `tenant_id` is `None`.

```python
from django_rls_tenants.exceptions import NoTenantContextError

# NoTenantContextError: use admin_context() for admin access
with tenant_context(tenant_id=None):  # raises NoTenantContextError
    ...
```

## admin_context

Enables admin bypass -- all tenant data is visible:

```python
from django_rls_tenants import admin_context

with admin_context():
    # Sees data from ALL tenants
    all_orders = Order.objects.all()
    total = all_orders.count()
```

**Parameters:**

| Parameter | Type  | Default     | Description |
|-----------|-------|-------------|-------------|
| `using`   | `str` | `"default"` | Database alias. |

**Behavior:**

- Sets `rls.is_admin = 'true'` and clears `rls.current_tenant`.
- Clears the internal `ContextVar` state so `RLSManager.get_queryset()` does not
  add any tenant filter (admin sees all rows).
- Saves and restores previous GUC values and state on exit (supports nesting).

## Nesting

Context managers support arbitrary nesting. Each level saves the previous state and
restores it on exit:

```python
with admin_context():
    # Admin: sees everything
    all_count = Order.objects.count()

    with tenant_context(tenant_id=1):
        # Scoped to tenant 1
        t1_count = Order.objects.count()

        with tenant_context(tenant_id=2):
            # Scoped to tenant 2
            t2_count = Order.objects.count()

        # Back to tenant 1
        assert Order.objects.count() == t1_count

    # Back to admin
    assert Order.objects.count() == all_count
```

Save and restore happen in **both** GUC modes. With `USE_LOCAL_SET=True` the previous
values are re-issued with `SET LOCAL` when the inner block exits, so the enclosing block
keeps its own scope for the rest of the transaction; PostgreSQL then drops everything at
`COMMIT` / `ROLLBACK` as usual.

!!! warning "Before 1.3.2"
    With `USE_LOCAL_SET=True`, versions up to 1.3.1 did not restore on block exit at all.
    An inner `admin_context()` left `rls.is_admin = 'true'` on the connection until the
    transaction ended, while `get_current_tenant_id()` still reported the outer tenant.
    Raw SQL and plain managers saw every tenant for the rest of the transaction (#94).

## Exceptions Inside a Context Block

An exception raised inside a block resets the Python state and attempts to restore the
previous GUC values. Restoration depends on the transaction state:

- **A Python exception** (`ValueError`, a failed assertion, ...) leaves the database
  transaction healthy, so the outer scope is restored normally and the enclosing block
  continues with its own tenant.
- **A failed SQL statement** (`IntegrityError`, `DataError`, ...) puts the PostgreSQL
  transaction into the aborted state, where every further statement is rejected. The
  library suppresses database errors from the restore and lets *your* exception propagate instead of
  replacing it with `InternalError: current transaction is aborted`. Nothing is lost:
  PostgreSQL rolls back `set_config()` and `SET LOCAL` changes together with the
  transaction, or with the savepoint when the block sits inside a nested
  `transaction.atomic()`, so the outer scope is reinstated by the rollback itself.

```python
with transaction.atomic(), tenant_context(tenant_id=1):
    try:
        with transaction.atomic(), admin_context():
            Order.objects.create(...)   # raises IntegrityError
    except IntegrityError:
        ...                             # the real error, not "transaction is aborted"
    # Back to tenant 1: the savepoint rollback restored the GUCs.
    Order.objects.count()
```

## Async Code

*Added in 1.4.0.* `async_tenant_context()` and `async_admin_context()` are the
`async with` counterparts of the two managers above, for async views, consumers and
tasks:

```python
from django_rls_tenants import async_admin_context, async_tenant_context


async def order_count(request, tenant_id):
    async with async_tenant_context(tenant_id):
        count = await Order.objects.acount()
    async with async_admin_context():
        total = await Order.objects.acount()
    return JsonResponse({"tenant": count, "all": total})
```

Django has no asynchronous database cursor: every async ORM call is a
`sync_to_async(..., thread_sensitive=True)` hop to the thread that owns the connection.
The async managers work the same way, with one hop on entry (snapshot and set the GUCs)
and one on exit (restore), so they are **not** non-blocking. What they give you is
`async with` ergonomics, and tenant state (`ContextVar`s) that is set and reset in your
coroutine itself, so the auto-scope filter and [strict mode](#strict-mode) apply to
`acount()`, `aget()`, `async for` and the rest of the async ORM.

[Nesting](#nesting) and [exceptions](#exceptions-inside-a-context-block) behave exactly as
for the sync managers, including the restore in both GUC modes. The restore also runs when
the task is cancelled inside the block, or while the entry hop is still running.

The generic layer has the same pair, `async_rls_context()` and `async_bypass_flag()`, plus
the primitives `aset_guc()`, `aget_guc()`, `aclear_guc()` and `arestore_guc()` (see the
[API reference](../reference/api.md#async-guc-helpers)).

For tests, the testing helpers have async counterparts. [Async Tests](testing.md#async-tests)
also explains which database connection an `async def` test uses.

### `USE_LOCAL_SET=True` in async code

`SET LOCAL` needs `transaction.atomic()` open on the thread that runs the database calls,
and a coroutine cannot open one (`atomic()` on the event-loop thread raises
`SynchronousOnlyOperation`). With `USE_LOCAL_SET=True` the async managers therefore raise
`RLSConfigurationError` unless the coroutine is driven by `async_to_sync` from synchronous
code that is already inside `transaction.atomic()`:

```python
from asgiref.sync import async_to_sync
from django.db import transaction

with transaction.atomic():
    async_to_sync(do_tenant_work)()   # async_tenant_context() inside works
```

Under an ASGI request or a bare event loop, use the default `USE_LOCAL_SET=False`. The
generic `async_rls_context()` and `async_bypass_flag()` do not check this: with
`is_local=True` outside a transaction the `RuntimeError` from `set_guc()` propagates.

### One context per task

Sibling tasks of one request share the request's database connection. Their GUC writes
interleave, so **do not** run blocks for different tenants concurrently:

```python
# Do not: both tasks write rls.current_tenant on the same connection.
await asyncio.gather(
    report_for(tenant_a),   # async with async_tenant_context(tenant_a.pk): ...
    report_for(tenant_b),   # async with async_tenant_context(tenant_b.pk): ...
)
```

Model queries stay scoped, because each task has its own `ContextVar` state and the ORM
auto-scope filter uses it. Raw SQL, `safe_tenant_sql()` and M2M through tables rely on
the GUCs alone and are not protected. Run such work one tenant after another.

### Mixing sync and async code

The sync and async managers nest in both directions. A sync `tenant_context()` inside a
`sync_to_async(..., thread_sensitive=True)` call runs on the same connection and restores
the outer async block when it exits; an async block run with `async_to_sync` inside a sync
`tenant_context()` restores the sync scope. Always pass `thread_sensitive=True` (the
default): a different thread means a different connection, which does not carry the GUCs.

## @with_rls_context Decorator

The `with_rls_context` decorator automatically extracts a user argument from a
function's signature and sets the appropriate RLS context:

```python
from django_rls_tenants import with_rls_context


@with_rls_context
def process_order(request, as_user):
    # RLS context set automatically based on as_user
    orders = Order.objects.all()
    return process(orders)
```

### How It Works

1. At **decoration time**: rejects generator functions (see
   [What the Decorator Rejects](#what-the-decorator-rejects)), caches
   `inspect.signature()` of the wrapped function, and picks an `async def` wrapper for an
   `async def` function (see [Async Functions](#async-functions)), a plain one otherwise.
2. At **call time** (for an `async def`, when the call is awaited): extracts the user
   argument by name from `*args`/`**kwargs`.
3. If the user is an **admin** (`is_tenant_admin=True`): wraps in `admin_context()`
   (`async_admin_context()` for an `async def`).
4. If the user is a **tenant user**: wraps in `tenant_context(user.rls_tenant_id)`
   (`async_tenant_context()` for an `async def`).
5. If the user is **`None`**: logs a warning and proceeds without context (fail-closed).

### Async Functions

*Added in 1.4.0.* On an `async def` function the decorator returns an `async def`
wrapper, which enters `async_admin_context()` or `async_tenant_context()` around the
awaited body, so the context covers every `await` in it:

```python
from django.contrib.auth.decorators import login_required


@with_rls_context
async def order_count(as_user):
    return await Order.objects.acount()


@login_required  # an AnonymousUser has no is_tenant_admin / rls_tenant_id
async def dashboard(request):
    user = await request.auser()
    return JsonResponse({"orders": await order_count(as_user=user)})
```

- The wrapper is itself a coroutine function, so Django (async views and async-aware
  decorators such as `@require_GET`) and `asyncio` treat the decorated function as async.
- Calling the function only creates the coroutine. The user argument is read, the context
  entered, the body run and the context restored when it is awaited, all in the awaiting
  task. A `NoTenantContextError` for a non-admin user without a tenant is raised by the
  `await`.
- `is_tenant_admin` and `rls_tenant_id` are read in the coroutine, so they must not query
  the database. Pass a loaded user: in an async view use `await request.auser()`, not
  `request.user`, a lazy object that queries on first access and raises
  `SynchronousOnlyOperation` in async code.
- Everything in [Async Code](#async-code) applies: one thread hop on entry and one on exit,
  the [`USE_LOCAL_SET=True` limitation](#use_local_settrue-in-async-code), and
  [one context per task](#one-context-per-task). In particular, do not run decorated calls
  for different users concurrently, as in
  `asyncio.gather(order_count(as_user=a), order_count(as_user=b))`: the tasks share the
  request's connection, so raw SQL and M2M through tables can see the other user's rows.

!!! warning "Before 1.4.0"
    The decorator always returned a sync wrapper. On an `async def` it entered and left the
    context before the coroutine ran, so the body ran under the caller's context instead.
    Called from synchronous code inside `admin_context()` (for example through
    `async_to_sync`), a coroutine decorated for a tenant user saw **every tenant's rows**.
    Awaited from async code, the call failed with `SynchronousOnlyOperation` (#39).

### What the Decorator Rejects

Both cases below raise `TypeError` instead of running a body outside the context.

- **Generator functions**, sync (`def` with `yield`) or async (`async def` with `yield`),
  fail at decoration time. A generator's body runs while the caller iterates, after the
  decorated call has returned, so no wrapper can keep a context around it. Enter the
  context inside the generator instead:

    ```python
    def export_orders(as_user):
        if as_user.is_tenant_admin:
            ctx = admin_context()
        else:
            ctx = tenant_context(as_user.rls_tenant_id)
        with ctx:
            yield from Order.objects.iterator()
    ```

    While the generator is suspended between items, its context is active for the caller
    too, and it is only restored when the generator finishes or is closed. Iterate it to
    the end, or close it (`contextlib.closing()`; Django closes a `StreamingHttpResponse`
    body for you).

- **A coroutine returned by a function the decorator saw as synchronous** fails at call
  time, after the coroutine is closed. This happens for an object with
  `async def __call__`, or for an `async def` under another decorator that returns a plain
  function. Put `@with_rls_context` directly on the `async def`, as the innermost
  decorator.

### Default Parameter Name

By default, the decorator looks for a parameter named by the `USER_PARAM_NAME` setting
(default: `"as_user"`):

```python
@with_rls_context
def my_function(data, as_user):  # "as_user" matches the default
    ...
```

### Custom Parameter Name

Use `user_param` to specify a different parameter name:

```python
@with_rls_context(user_param="current_user")
def my_function(data, current_user):
    ...
```

### Signature Mismatch Warning

If the parameter is not found in the function signature, the decorator logs a warning
at decoration time and the function will always run without RLS context (fail-closed):

```python
@with_rls_context
def my_function(data):  # no "as_user" parameter -- warning logged
    ...
```

### Examples

```python
# Bare decorator (uses default USER_PARAM_NAME)
@with_rls_context
def create_order(request, as_user):
    Order.objects.create(title="New Order", amount=100)

# With explicit user_param
@with_rls_context(user_param="user")
def get_dashboard_data(user):
    return {
        "orders": Order.objects.count(),
        "invoices": Invoice.objects.count(),
    }

# Called as a regular function -- user is extracted automatically
create_order(request, as_user=tenant_user)
create_order(request, tenant_user)  # also works (positional)
```

## Strict Mode

When `STRICT_MODE=True`, queries on RLS-protected models raise
`NoTenantContextError` if no RLS context is active. Both `tenant_context()` and
`admin_context()` establish an active context, so queries inside these blocks
always pass the strict mode check.

```python
from django_rls_tenants import tenant_context, admin_context
from django_rls_tenants.exceptions import NoTenantContextError

# Without context -- raises NoTenantContextError (strict mode)
try:
    Order.objects.count()  # NoTenantContextError
except NoTenantContextError:
    pass

# With context -- works normally
with tenant_context(tenant_id=42):
    Order.objects.count()  # OK

with admin_context():
    Order.objects.count()  # OK
```

The `@with_rls_context` decorator also establishes an active context when a valid
user argument is provided, so decorated functions pass the strict mode check (for an
`async def`, for the whole awaited body).
`async_tenant_context()` and `async_admin_context()` set the flag in the coroutine, so the
async ORM passes the check inside them too (see [Async Code](#async-code)).

See [Configuration](../getting-started/configuration.md#strict_mode) for setup
instructions.

## Debug Logging

Both `tenant_context()` and `admin_context()` emit `DEBUG`-level log messages
on entry and exit. This is useful for tracing RLS context transitions in
management commands, Celery tasks, and service-layer code. No output is
produced unless you enable `DEBUG` level on the `django_rls_tenants` logger:

```python title="settings.py"
LOGGING = {
    "version": 1,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "django_rls_tenants": {
            "handlers": ["console"],
            "level": "DEBUG",
        },
    },
}
```

Example output:

```
DEBUG tenant_context entered: tenant_id=42, using=default
DEBUG tenant_context exited: tenant_id=42, using=default
DEBUG admin_context entered: using=default
DEBUG admin_context exited: using=default
```

## Multi-Database Support

All context managers accept a `using` parameter for multi-database setups:

```python
with tenant_context(tenant_id=42, using="replica"):
    orders = Order.objects.using("replica").all()

with admin_context(using="analytics"):
    data = Report.objects.using("analytics").all()
```

The `TenantQuerySet.for_user()` method automatically uses `self.db`, so chaining
`.using()` works correctly:

```python
Order.objects.using("replica").for_user(user)
```
