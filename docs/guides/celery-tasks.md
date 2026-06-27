# Celery Tasks

Celery tasks run outside the request/response cycle, so `RLSTenantMiddleware`
never sets the RLS context for them. A task that queries an RLS-protected model
without a context gets zero rows (fail-closed), or raises `NoTenantContextError`
when `STRICT_MODE` is enabled.

django-rls-tenants ships a **native Celery integration** that captures the
active tenant (or admin) context when a task is enqueued and restores it on the
worker before the task body runs — so you don't have to thread `tenant_id`
through every task by hand. The [manual pattern](#without-the-extra) is still
available if you'd rather not add the dependency.

!!! info "Sync only"
    v1.3.0 is synchronous-only. The context is restored on the worker thread,
    which an event loop does not propagate into coroutines, so `async def` task
    bodies are **not** supported. Keep RLS-touching task bodies synchronous.

## Install

The integration lives behind the `celery` extra, keeping Celery an optional
dependency:

```bash
pip install "django-rls-tenants[celery]"
```

Import it from `django_rls_tenants.contrib.celery` (never from the top-level
package — that stays Celery-free):

```python
from django_rls_tenants.contrib.celery import rls_task
```

## `@rls_task`

`@rls_task` is a drop-in replacement for `@shared_task`. Use it exactly the same
way; the only difference is that the task now carries the RLS context across the
enqueue → worker boundary:

```python
from django_rls_tenants.contrib.celery import rls_task


@rls_task
def process_orders() -> None:
    for order in Order.objects.all():   # scoped to the enqueuing tenant
        ...
```

Enqueue it from inside a context — usually the request context set by
`RLSTenantMiddleware`, or an explicit `tenant_context()`:

```python
# In a view, with the middleware active:
process_orders.delay()        # captures request.user's tenant

# Or explicitly:
with tenant_context(tenant.pk):
    process_orders.delay()    # worker runs the body under tenant_context(tenant.pk)
```

Notice there is **no `tenant_id` argument**. The context is captured
automatically, so the task signature stays about the work, not the plumbing.

!!! tip
    `@rls_task` accepts every `shared_task` option, bound tasks included:

    ```python
    @rls_task(bind=True, max_retries=3)
    def sync(self) -> None:
        ...
    ```

### How it works

On enqueue (`delay` / `apply_async`), the active context is serialised into the
task's message **headers** (`rls_tenant_id`, `rls_admin`). On the worker, the
task base class reads those headers and runs the body inside the matching
`tenant_context()` / `admin_context()`, which restores cleanly whether the body
returns or raises. An explicitly-passed header always wins over the captured
value, so you can override it per call if you ever need to.

If no context is active when the task is enqueued, nothing is captured and the
task runs unscoped (fail-closed) — unless you opt into
[`rls_require_context`](#requiring-a-context).

## The `RLSTask` base class

`@rls_task` is sugar for `shared_task(base=RLSTask)`. Use the base class
directly when you need a custom base — for example to set defaults shared by
several tasks:

```python
from celery import shared_task
from django_rls_tenants.contrib.celery import RLSTask


@shared_task(base=RLSTask)
def rebuild_index() -> None:
    ...
```

## Chains and groups

Context propagates through canvases (chains, groups, chords). When the worker
finishes one step and enqueues the next, the upstream task is still the *current*
task, so its headers are inherited even though its `tenant_context()` has already
closed:

```python
from celery import chain
from django_rls_tenants.contrib.celery import rls_task


@rls_task
def extract(): ...


@rls_task
def load(result): ...


with tenant_context(tenant.pk):
    chain(extract.s(), load.s()).apply_async()   # both steps run under tenant.pk
```

!!! warning "Every step must be an RLS task"
    Propagation only works for steps that use `@rls_task` / `RLSTask` (or the
    [`install()`](#global-escape-hatch-install) hook). A plain `@shared_task`
    in the middle of a chain breaks the context for itself **and** every step
    after it.

## Requiring a context

By default a task with no propagated context runs unscoped (RLS then returns
zero rows). For jobs that must never run tenant-blind, set `rls_require_context`
on a `RLSTask` subclass to fail fast instead:

```python
from celery import shared_task
from django_rls_tenants.contrib.celery import RLSTask


class StrictTask(RLSTask):
    rls_require_context = True


@shared_task(base=StrictTask)
def charge_invoices() -> None:
    ...
```

Enqueued without a context, `charge_invoices` raises `NoTenantContextError`
(carrying a `Hint:` on how to fix it) instead of running.

## Cross-tenant and scheduled tasks

For periodic (beat) tasks that operate across all tenants, enqueue under
`admin_context()` — it propagates as admin mode — then re-enter
`tenant_context()` for each tenant inside the body:

```python
from django_rls_tenants import admin_context, tenant_context
from django_rls_tenants.contrib.celery import rls_task


@rls_task
def nightly_billing() -> None:
    with admin_context():
        tenant_ids = list(Tenant.objects.values_list("pk", flat=True))
    for tenant_id in tenant_ids:
        with tenant_context(tenant_id):
            _bill_one_tenant()
```

!!! warning
    `admin_context()` bypasses tenant isolation entirely — it sees every
    tenant's data. Keep its body minimal: fetch the tenant list, then scope each
    operation back down with `tenant_context()`.

## Global escape hatch: `install()`

`@rls_task` / `RLSTask` is the recommended API, but you cannot always re-base a
task onto it (third-party tasks, a large legacy code base). `install()` wires the
same capture/restore globally via Celery signals
(`before_task_publish` + `task_prerun` / `task_postrun`), so context flows for
**all** tasks regardless of their base class:

```python
# In your Celery app module, once at startup:
from django_rls_tenants.contrib.celery import install

install()
```

It is idempotent (a repeated call does not double-wire) and reversible with
`uninstall()`. `install()` and the base class compose safely — `RLSTask`
instances keep managing their own context and are skipped by the signal
handlers, so there is no double-entry.

!!! note
    The signal hook fires on a real broker. In eager mode
    (`task_always_eager`), `before_task_publish` does not run, so prefer
    `@rls_task` / `RLSTask` when you want context propagation in eager-mode
    tests.

!!! warning "Call `uninstall()` at shutdown, not mid-flight"
    `uninstall()` can only unwind contexts entered on the calling thread (a
    `ContextVar` token is thread-bound and database connections are
    thread-local). Calling it while tasks are still running on worker threads
    leaves those in-flight contexts to unwind on their own thread when the task
    finishes — so disconnect at shutdown, or from the worker thread between
    tasks.

## Multi-database tasks

The propagated context is restored on the **`default`** database alias only.
`RLSTenantMiddleware` sets the RLS GUC on every alias in
`RLS_TENANTS["DATABASES"]`, but the task integration restores a single alias, so
queries a task runs against any *other* alias are **not** scoped unless you
re-enter the context for that alias yourself. Pass `using="..."` to the context
manager inside the task body to scope queries on a specific alias:

```python
@rls_task
def replicate() -> None:
    with tenant_context(get_current_tenant_id(), using="replica"):
        Order.objects.using("replica").all()
```

!!! warning "Non-default aliases are not auto-scoped"
    A task body that queries a replica or secondary database without an explicit
    `tenant_context(..., using="<alias>")` runs **unscoped** on that alias (RLS
    then returns zero rows, or every row in admin mode). Wrap every non-default
    alias the task touches, as shown above.

See [Context Managers](context-managers.md#multi-database-support) for details.

## Security model

A few properties are worth keeping in mind for a multi-tenant deployment:

- **Task headers are trusted.** `rls_tenant_id` / `rls_admin` travel in the task
  message, so anything that can publish to your broker can ask a worker to run
  under any tenant — or as a cross-tenant admin (`rls_admin: true`). This is the
  same trust Celery already places in task arguments; treat broker credentials
  as security-sensitive and keep the broker off untrusted networks.
- **Admin context flows down a canvas.** A task running under `admin_context()`
  propagates admin mode to the steps it enqueues (chain / group / chord links),
  because each downstream step inherits the upstream task's headers. If you link
  an unrelated task to an admin task it inherits admin too — pass an explicit
  `headers={"rls_tenant_id": tenant_id}` to scope it down, or don't enqueue it
  from inside an admin context.

## Without the extra

If you'd rather not install Celery as a managed dependency of this library, you
can keep the context wiring in your own code. Pass the tenant **id** (never a
model instance — Celery serialises arguments, and a serialised instance is
wasteful and can go stale) and wrap the body in `tenant_context()`:

```python
from celery import shared_task
from django_rls_tenants import tenant_context


@shared_task
def process_orders(tenant_id: int) -> None:
    with tenant_context(tenant_id):
        for order in Order.objects.all():   # scoped to this tenant
            ...
```

```python
# Enqueue from a view:
process_orders.delay(request.user.rls_tenant_id)
```

To avoid repeating the `with` block, a thin decorator that reads `tenant_id`
from the first argument works for unbound tasks:

```python
import functools
from django_rls_tenants import tenant_context


def with_tenant_context(func):
    @functools.wraps(func)
    def wrapper(tenant_id, *args, **kwargs):
        with tenant_context(tenant_id):
            return func(tenant_id, *args, **kwargs)
    return wrapper


@shared_task
@with_tenant_context
def process_orders(tenant_id: int) -> None:
    for order in Order.objects.all():   # already scoped by the decorator
        ...
```

!!! warning
    This decorator assumes `tenant_id` is the **first positional argument**, so
    it does not work with bound tasks (`@shared_task(bind=True)`) — there Celery
    passes the task instance first. For bound tasks, keep the explicit
    `with tenant_context(tenant_id):` block in the body.

!!! note
    See [Context Managers](context-managers.md) for the full `tenant_context()`
    and `admin_context()` API — nesting, debug logging, and strict-mode
    interaction.
