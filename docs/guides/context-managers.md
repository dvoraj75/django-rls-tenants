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

The enclosing block's values come back in **both** GUC modes. With session GUCs the inner
block snapshots the previous values and writes them back on exit. With `USE_LOCAL_SET=True`
on an alias in `RLS_TENANTS["DATABASES"]` the blocks write nothing on entry, inside
`transaction.atomic()` or not, and the GUC affinity guard applies whichever context is
active (see [below](#use_local_settrue-and-transactions)); PostgreSQL drops the
transaction-local values at `COMMIT` / `ROLLBACK` as usual. *Changed in 1.5.0:* before,
inside `atomic()` this mode snapshotted, wrote with `SET LOCAL` on entry and re-issued the
previous values with `SET LOCAL` on exit.

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

## `USE_LOCAL_SET=True` and Transactions

*Changed in 1.4.0 outside `atomic()`, in 1.5.0 inside it.* `SET LOCAL` needs a
transaction. On an alias in `RLS_TENANTS["DATABASES"]` the managers do not use it: they
write nothing on entry, inside `transaction.atomic()` or not, and only set the Python-side
context. Before 1.4.0 they raised `RuntimeError` outside `atomic()`; before 1.5.0 they
wrote `SET LOCAL` values inside it. The
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard)
applies the values transaction-locally, both GUCs in one `set_config()` statement: in a
transaction of its own for a statement run in autocommit (`BEGIN`, the `set_config()`
statement, your statement, `COMMIT`: three extra round trips), and once per transaction,
before the first statement that needs them, in a transaction that is open already. Celery
tasks, management commands and scripts therefore need no `transaction.atomic()`:

```python
with tenant_context(tenant_id=42):  # no atomic() needed
    orders = list(Order.objects.all())
```

Put a batch of statements in one `transaction.atomic()` when the round trips matter.
Opened inside the block, it costs one `set_config()` per transaction. Opened around the
block, the block costs one `set_config()` before its first statement and, when it exits
with the transaction still open, one more: the guard puts the enclosing context's values
back, or clears them for the session when there is none, so SQL that bypasses Django's
cursor (a driver cursor, `callproc()`) never sees a block's values after it. Before 1.5.0
this was about six small statements per block (two `current_setting()` reads and two
`SET LOCAL` statements on entry, two on exit). After an earlier context has ended on that
connection the guard first clears the session's values, one more statement, and a loop
that enters `atomic()` and a context on every iteration pays it every time. The same
holds for `admin_context()`,
`@with_rls_context`, the `for_user()` scope and the testing helpers. When a block ends,
what the session holds for itself counts as unknown, and the next statement run outside any
context, in autocommit or in a transaction, clears it first (one statement), in case code
inside the block wrote the GUCs for the session. Such a write does not narrow the block's
context in autocommit: the guard's values override it, so change the context with
`tenant_context()` / `admin_context()`. See [Code Outside a Request
Transaction](connection-pooling.md#code-outside-a-request-transaction) for the cost by
placement, the query counts, and where the guard stops.

One limit comes with writing nothing on entry (since 1.5.0 inside `atomic()` too): inside
a block, until its first statement through Django's cursor, SQL that bypasses it (a
driver cursor, `callproc()`, the `FETCH` of an already open server-side cursor) sees the
enclosing context's values, or none, not the block's. Run one ORM query first if such SQL
needs the block's values. Code in a block that
writes the admin or tenant GUC for the session (`set_guc()`, `rls_context()` without
`is_local=True`) inside `atomic()` also loses that write when the block exits with no
enclosing context: the exit clears both GUCs for the session.

On an alias outside `RLS_TENANTS["DATABASES"]`, which the guard leaves alone, nothing
changes: the managers snapshot, write `SET LOCAL` on entry and restore on exit, so a block
entered outside `atomic()` still raises `RuntimeError` ("Cannot use SET LOCAL for ...
outside a transaction"), and the async managers raise `RLSConfigurationError`.

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
also explains which database connection an `async def` test uses. For running the project on
an ASGI server, see [Async Deployment](async.md).

### `USE_LOCAL_SET=True` in async code

*Changed in 1.4.0.* A coroutine cannot open `transaction.atomic()` (`atomic()` on the
event-loop thread raises `SynchronousOnlyOperation`), so the thread that runs the database
calls is normally in autocommit. With `USE_LOCAL_SET=True` the async managers write
nothing there: they set the Python-side context, and the GUC affinity guard gives each
statement the context in a transaction of its own (see
[`USE_LOCAL_SET=True` and Transactions](#use_local_settrue-and-transactions)).
That works in an ASGI request, a bare event loop and a script run with `asyncio.run()`:

```python
async with async_tenant_context(tenant_id):  # no atomic() needed
    count = await Order.objects.acount()
```

When the coroutine is driven by `async_to_sync` from synchronous code that is already
inside `transaction.atomic()`, the managers still write nothing: the guard sets the values
in that transaction, as it does for the sync managers (*changed in 1.5.0*; before, the
managers wrote `SET LOCAL` values there):

```python
from asgiref.sync import async_to_sync
from django.db import transaction

with transaction.atomic():
    async_to_sync(do_tenant_work)()   # the guard sets the values in this transaction
```

On an alias outside `RLS_TENANTS["DATABASES"]`, which the guard leaves alone, the async
managers still need that transaction and raise `RLSConfigurationError` without one; add
the alias to the list, or run the block through `async_to_sync` inside `atomic()`. The
generic `async_rls_context()` and `async_bypass_flag()` do not check this: with
`is_local=True` outside a transaction the `RuntimeError` from `set_guc()` propagates.

### Concurrent tasks

Sibling tasks of one request share the request's database connection, but each has its
own `ContextVar` state. Before each statement, the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) applies the context of
the task that runs it, so blocks for different tenants can run concurrently:

```python
await asyncio.gather(
    report_for(tenant_a),   # async with async_tenant_context(tenant_a.pk): ...
    report_for(tenant_b),   # async with async_tenant_context(tenant_b.pk): ...
)
```

That covers raw SQL, `safe_tenant_sql()` and M2M through tables, which rely on the GUCs
alone. It does not cover:

- bypass flags and other GUCs set with `async_bypass_flag()` / `async_rls_context()`: they
  stay on the shared connection while the block runs, and sibling tasks see them. Do not
  run such a block concurrently with work that must not get the bypass;
- a block that exits in another `contextvars.Context` than it entered in (a generator
  suspended inside it and closed elsewhere): it restores, on the session it entered on,
  the values of the context it was entered in while that context is live, even if another
  context is current there;
- SQL that bypasses Django's execute wrappers: a driver cursor, `callproc()`, the driver's
  `copy` methods, and the `FETCH`es of a server-side cursor (`iterator()`) opened inside a
  transaction, whose rows PostgreSQL computes as they are fetched. Outside a transaction
  (autocommit, Django's default) it computes them when the cursor opens.

A task that a block starts and does not await gets the block's context only while the
block is live: once the block has ended, that task's statements run without it
(fail-closed). Enter the context inside the task if it outlives the block.

A `sync_to_async` call cancelled while its function is still inside a block (an
`asyncio.wait_for()` that times out, say) leaves the caller's own context in place. A
coroutine outside any context and outside a request that `RLSTenantMiddleware` serves is
the exception: it runs with the cancelled call's block until that block ends, so do not
run code there that must not have it, or enter a context first.

### Mixing sync and async code

The sync and async managers nest in both directions. A sync `tenant_context()` inside a
`sync_to_async(..., thread_sensitive=True)` call runs on the same connection and restores
the outer async block when it exits; an async block run with `async_to_sync` inside a sync
`tenant_context()` restores the sync scope. Prefer `thread_sensitive=True` (the default): a
different thread means a different connection. The
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) applies your context
there too, but that connection is outside any transaction your code has open (with
`USE_LOCAL_SET=True`, each of its statements gets a transaction of its own).

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
  the [`USE_LOCAL_SET=True` behaviour](#use_local_settrue-in-async-code), and
  [concurrent tasks](#concurrent-tasks). Decorated calls for different users can run
  concurrently, as in `asyncio.gather(order_count(as_user=a), order_count(as_user=b))`,
  within the limits listed there.

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

*Changed in 1.4.0:* `using` names the alias a context manager writes its GUCs to on entry
and restores on exit. Every alias in `RLS_TENANTS["DATABASES"]` follows the context as well:
the [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) applies it before
each statement there. `tenant_context(42)` therefore also scopes a read that a router sends
to a configured replica, and `admin_context(using="analytics")` gives the admin bypass on
every configured alias. An alias outside `RLS_TENANTS["DATABASES"]` gets the GUCs only when
you name it in `using`. With `USE_LOCAL_SET=True` the configured aliases follow the context
too, with transaction-local values: the guard sets them per statement in autocommit, or
once per transaction opened inside the context. An alias outside the list gets them only
when you name it in `using` and have a transaction open on it
(`transaction.atomic(using=...)`).

!!! warning "`using` does not limit a context to one alias"
    `using` names the alias the GUCs are written to first, not the aliases the context
    reaches. `admin_context(using="analytics")` also gives the admin bypass on every alias in
    `RLS_TENANTS["DATABASES"]`, `default` included, even when `analytics` is not listed.
    Do not enter an admin context for an alias you do not need it on.
