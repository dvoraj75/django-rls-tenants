# Async Deployment

*Added in 1.4.0.* This page is for running a project that uses django-rls-tenants on an
ASGI server: which server settings matter, how database connections behave, which
`RLS_TENANTS` and `DATABASES` combinations are safe, how the tenant context travels between
async and sync code, and what `USE_LOCAL_SET=True` means when Django has no async
transactions. The pages it links to hold the details.

!!! tip "Short version"
    - Any ASGI server works. Run Django's own `asgi.py` application on an asyncio event loop.
    - Keep `CONN_MAX_AGE = 0` under ASGI. For connection reuse, use Django's connection pool
      with `reset_rls_gucs`, or PgBouncer in transaction mode with `USE_LOCAL_SET=True`.
    - Use `async_tenant_context()` / `async_admin_context()` in async code, never the sync
      managers in a coroutine.
    - Keep database work on the request's thread: `sync_to_async()` with its default
      `thread_sensitive=True`.
    - With `USE_LOCAL_SET=True`, a request on the middleware's async path has no request
      transaction. Put writes that belong together in `transaction.atomic()` inside a
      synchronous function.

## How async Django runs database code

Django has no asynchronous database cursor. Every async ORM call (`aget()`, `acount()`,
`async for`, ...) runs the synchronous implementation through
`sync_to_async(..., thread_sensitive=True)`, a hop to a thread. Django's connections are
per thread, so that thread decides which connection a query uses.

Under ASGI, Django gives each request a thread of its own: its handler wraps the request in
asgiref's `ThreadSensitiveContext`, so every thread-sensitive hop of that request runs on
one thread, and the thread is shut down when the request ends. A request therefore uses
**one connection** for everything it does on that thread, the middleware's hops included.
This is the invariant the library relies on:

- `RLSTenantMiddleware` reads `request.user` and sets the GUCs in one hop, on the request's
  connection, then sets the tenant `ContextVar`s in the request coroutine. One more hop
  clears the GUCs at the end. See [Running under ASGI](middleware.md#running-under-asgi).
- The async context managers and the `aset_guc()` family make the same kind of hop. They
  are not non-blocking: they give `async with` ergonomics, and they set the tenant state in
  your coroutine so the async ORM is scoped (see [Async Code](context-managers.md#async-code)).
- Code that runs on any other thread uses that thread's connection. Examples are an error
  page that Django builds on a worker thread, `sync_to_async(..., thread_sensitive=False)` and
  `asyncio.to_thread()`. The [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard)
  applies the context that code was handed before each of its statements.

Sibling tasks of one request, such as the coroutines of an `asyncio.gather()`, share the
request's thread and connection but each keeps its own context. The guard applies the
running task's context before each statement (see
[Concurrent tasks](context-managers.md#concurrent-tasks) for the limits).

## ASGI servers

The library needs nothing server-specific. Point the server at the project's `asgi.py`
(`get_asgi_application()`) and keep `RLSTenantMiddleware` in `MIDDLEWARE` as under WSGI.

=== "Uvicorn"

    ```bash
    python -m uvicorn myproject.asgi:application --host 0.0.0.0 --port 8000 --workers 4
    ```

    In production you can also run it under Gunicorn with the `uvicorn-worker` package:

    ```bash
    python -m gunicorn myproject.asgi:application -k uvicorn_worker.UvicornWorker --workers 4
    ```

    Django does not implement the ASGI `lifespan` protocol. Uvicorn logs that and carries
    on; `--lifespan off` silences it.

=== "Daphne"

    ```bash
    daphne -b 0.0.0.0 -p 8000 myproject.asgi:application
    ```

    Adding `"daphne"` at the top of `INSTALLED_APPS` (with `ASGI_APPLICATION` set) makes
    `manage.py runserver` serve the project through ASGI. That is the quickest way to try the
    middleware's async path in development.

=== "Hypercorn"

    ```bash
    hypercorn myproject.asgi:application --bind 0.0.0.0:8000 --workers 4
    ```

    Keep an asyncio worker class (`asyncio`, the default, or `uvloop`). Django's ASGI handler
    and asgiref's `sync_to_async` need an asyncio event loop, so `trio` does not work.

What matters for RLS is the same on every server:

- **Which middleware path serves the request.** Django runs `RLSTenantMiddleware` on its
  native async path only when every middleware listed after it can run asynchronously.
  Django's own middleware can. One synchronous-only middleware after it puts it on the
  synchronous path, run inside a thread hop. Both paths are correct. They differ with
  `USE_LOCAL_SET=True`, where only the synchronous path has a request transaction (see
  [below](#set-local-without-async-transactions)). To see which path you get, set
  `DEBUG = True` and the `django.request` logger to `DEBUG`. When the application loads,
  Django logs `Asynchronous handler adapted for middleware <path>.` for the last
  synchronous-only middleware in `MIDDLEWARE`. If that line names a middleware listed after
  `RLSTenantMiddleware`, the synchronous path serves your requests.
- **Connections per process.** Each request in flight holds a connection on its own thread.
  Each worker process can therefore have as many connections open as requests it serves at
  once, plus one per thread of the event loop's default executor (about
  `min(32, CPU count + 4)` threads unless the server sets another executor). Those threads
  run Django's ASGI error pages and `thread_sensitive=False` work. Django closes or returns
  only the request thread's connections when a request ends, so a connection opened on an
  executor thread stays with that thread. Without a pool nothing caps the total: limit the
  requests in flight at the server (Uvicorn's `--limit-concurrency`, for one), or use
  Django's pool, whose `max_size` caps the connections. Size the pool (one per alias per
  process) for one process, and PostgreSQL's `max_connections` for all worker processes
  together.
- **WebSockets.** Django's ASGI application serves HTTP only, so `RLSTenantMiddleware`
  never sees a WebSocket connection. A Django Channels consumer must set the context
  itself: enter `async_tenant_context()` / `async_admin_context()` in each handler, based
  on the user Channels' authentication middleware put in `scope["user"]`.

## Connections and pooling

Django's documentation says to disable persistent connections under ASGI and to use a
connection pool instead. Each request's thread ends with the request, so a connection kept
with `CONN_MAX_AGE` is never reused by a later request.

The table lists the combinations and what each needs.

| Connection setup | `USE_LOCAL_SET=False` (default) | `USE_LOCAL_SET=True` |
|---|---|---|
| Direct to PostgreSQL, `CONN_MAX_AGE = 0` (Django's default) | **Safe.** The recommended starting point. Each request opens a connection and closes it at the end. | **Safe.** Each autocommit statement run under a context costs three extra round trips (see [below](#set-local-without-async-transactions)). |
| Direct to PostgreSQL, `CONN_MAX_AGE > 0` | **Avoid under ASGI.** Django advises against it and the connections are not reused. `W004` warns about it. | **Avoid under ASGI**, for the same reason. |
| Django's connection pool (`OPTIONS["pool"]`, psycopg 3) | **Safe.** The library clears every policy GUC on each checkout. Also set `"reset": reset_rls_gucs`, so idle connections hold nothing; `W010` asks for it. | **Safe.** The tenant and admin GUCs are transaction-local, and the checkout clear covers bypass flags left at session level. |
| PgBouncer, transaction mode | **Unsafe.** Session GUCs travel with the server connection to the next client, and nothing in Django sees that hand-off. | **Safe, and the only safe choice.** Also set `DISABLE_SERVER_SIDE_CURSORS = True` on the alias, as Django requires for transaction pooling. |
| PgBouncer, session mode | **Safe.** One client connection is one server session. The middleware clears the GUCs at the end of each request, and PgBouncer's `server_reset_query` (`DISCARD ALL` by default) resets the session when Django disconnects. | **Safe.** |

Notes on the rows:

- **Django's connection pool** needs Django 5.1 or later and psycopg 3 with
  `psycopg[pool]`. With psycopg2, Django refuses to connect
  (`ImproperlyConfigured: Database pooling requires psycopg >= 3`). Set `max_size` for the
  requests one process serves at once, plus the executor threads above. A request that
  finds the pool empty waits up to the pool's `timeout` and then fails. See
  [Django's Connection Pool](connection-pooling.md#djangos-connection-pool) for the
  checkout clear and [Clearing on Return](connection-pooling.md#clearing-on-return-reset_rls_gucs)
  for `reset_rls_gucs`.
- **PgBouncer in transaction mode** hands the server connection to another client at every
  transaction end. Only transaction-local GUCs are safe there. See
  [Transaction Mode](connection-pooling.md#transaction-mode-recommended).
- **Bypass flags** are session-scoped by default in both modes. Behind a transaction-mode
  pooler, set them with the sync `bypass_flag(..., is_local=True)` or
  `rls_context(..., is_local=True)` inside `transaction.atomic()`, in a synchronous
  function. `async_bypass_flag(is_local=True)` fails in a coroutine, which has no
  transaction open.
- `RLS_TENANTS["DATABASES"]` must list every alias the async code queries, replicas
  included. The guard, the checkout clear and the async managers' `USE_LOCAL_SET` support
  cover only the listed aliases.

## Context propagation across `sync_to_async` and `async_to_sync`

The library keeps the tenant ID, the "context active" flag that strict mode reads, and the
values the guard applies in `ContextVar`s. Python and asgiref copy `ContextVar`s in these
ways:

| Where the code runs | Context it gets | Database connection |
|---|---|---|
| `await coro()` in the same task | The caller's own context | The request's |
| `asyncio.create_task()`, `asyncio.gather()`, `TaskGroup` | A copy taken when the task is created; its changes never flow back | The request's |
| `sync_to_async(fn)` (`thread_sensitive=True`, the default) | A copy; asgiref copies values the function changed back to the caller | The request's, on the request's thread |
| `sync_to_async(fn, thread_sensitive=False)` | A copy; changed values are copied back, as above | That thread's own connection |
| `asyncio.to_thread()` | A copy; its changes never flow back | That thread's own connection |
| `async_to_sync(coro)` from sync code | A copy; changed values are copied back | The calling thread's: its hops return to that thread |
| `loop.run_in_executor()`, `ThreadPoolExecutor.submit()`, `threading.Thread` | **No copy**: the thread's own context, normally empty[^thread] | That thread's own connection |

[^thread]: On Python 3.14, a `threading.Thread` copies the caller's context when
    `sys.flags.thread_inherit_context` is on, which is the default only in the free-threaded
    build.

What this means in practice:

- **Set the context in the coroutine that does the work.** `async with
  async_tenant_context(...)` sets the `ContextVar`s in your coroutine, so every `await`, task
  and hop started inside the block inherits them. A sync `tenant_context()` is fine inside a
  function run with `sync_to_async`, because it begins and ends within that call. Do not
  set the state functions (`set_current_tenant_id()` and friends) inside a hop: the token
  belongs to a copied context and cannot be reset outside it (#58).
- **Work on another thread gets the context it was handed**, if a context manager,
  `for_user()` or the middleware set that context up. The guard applies it to that thread's
  connection before each statement, while the context's block is live. A context built with
  the state functions, and GUCs set with `set_guc()` / `rls_context()`, do not reach other
  threads. Work started without a copy (`run_in_executor()`, a bare `ThreadPoolExecutor`)
  runs with no context and sees no rows. Open a context inside it, or pass one with
  `contextvars.copy_context().run` (see
  [Work handed to another thread](../troubleshooting.md#work-handed-to-another-thread-sees-no-rows)).
- **A task that outlives its block loses the context.** A task started inside a block and
  not awaited there runs without the context once the block has ended (fail-closed). Enter
  the context inside the task.
- **Sibling tasks may hold different contexts**, such as a report per tenant run with
  `asyncio.gather()`. The guard applies each task's context before its statements. Bypass
  flags and SQL that bypasses Django's execute wrappers are not covered; read
  [Concurrent tasks](context-managers.md#concurrent-tasks) before fanning out.
- **Mixed nesting works both ways.** A sync block inside an async one, or an async block
  run with `async_to_sync` inside a sync one, restores the outer scope on exit (see
  [Mixing sync and async code](context-managers.md#mixing-sync-and-async-code)).
- **Streaming responses run after the middleware.** The body of a
  `StreamingHttpResponse` is iterated after the request's context has ended, so its queries
  see no rows, or raise `NoTenantContextError` under `STRICT_MODE`. Open
  `async with async_tenant_context(...)` inside an async generator, or `tenant_context()`
  inside a synchronous one.

## `SET LOCAL` without async transactions

Django has no async transaction API. `transaction.atomic()` cannot be entered in a
coroutine (it raises `SynchronousOnlyOperation`), and Django refuses `ATOMIC_REQUESTS` for
async views (`You cannot use ATOMIC_REQUESTS with async views.`). A transaction in async
code therefore always lives inside a synchronous function that you call with
`sync_to_async`.

With **`USE_LOCAL_SET=False`** (the default) this does not matter for RLS. The middleware's
first hop writes session GUCs on the request's connection, every statement on that
connection sees them, and the last hop clears them. Statements run in autocommit with no
extra cost.

With **`USE_LOCAL_SET=True`** a transaction-local value needs a transaction to live in, and
on the middleware's async path there is none for the request:

- The middleware opens no transaction and writes no GUC there. It only records the
  request's context. A request on that path is therefore not atomic, unlike one on the
  synchronous path (see [`USE_LOCAL_SET` and ASGI](middleware.md#use_local_set-and-asgi)).
  Django also cancels an async view when the client disconnects, so a request the client
  abandons can stop between two writes that have each committed.
- Each statement that runs in autocommit under a context gets a transaction of its own from
  the guard: `BEGIN`, one `set_config()` statement, the statement, `COMMIT`. That is three
  extra round trips per statement, and a wrapped statement counts as four queries in
  `connection.queries` and `assertNumQueries`.
- Inside a transaction, the guard writes the context's GUCs once, before the first statement
  that needs them, and again only when the context changes.

So group statements that belong together in one synchronous function with
`transaction.atomic()`. That makes them atomic and costs one `set_config()` statement for
the whole transaction instead of three round trips per statement:

```python
from asgiref.sync import sync_to_async
from django.db import transaction
from django.http import JsonResponse


def _close_order(order_id):
    with transaction.atomic():  # one set_config() for the whole transaction
        order = Order.objects.select_for_update().get(pk=order_id)
        order.status = "closed"
        order.save()
        AuditEntry.objects.create(order=order, action="close")


async def close_order(request, order_id):
    # The hop runs on the request's thread and inherits the request's context.
    await sync_to_async(_close_order)(order_id)
    return JsonResponse({"closed": order_id})
```

The same holds for a context the view enters itself:

```python
async with async_tenant_context(tenant_id):     # writes nothing with USE_LOCAL_SET=True
    count = await Order.objects.acount()         # BEGIN, set_config(), SELECT, COMMIT
    await sync_to_async(_close_order)(order_id)  # one set_config() inside the atomic()
```

A few more consequences:

- **Code driven by `async_to_sync` from inside `transaction.atomic()`** runs its hops on the
  calling thread, in that transaction. The async managers write nothing there either (since
  1.5.0; before, they wrote `SET LOCAL` values into it): the guard sets the values in that
  transaction, as for the sync ones (see
  [`USE_LOCAL_SET=True` in async code](context-managers.md#async-code)).
- **`thread_sensitive=False` work is outside your transaction.** It runs on another
  connection, so it does not see the transaction's uncommitted rows. With
  `USE_LOCAL_SET=True` each of its statements gets a transaction of its own.
- **An alias outside `RLS_TENANTS["DATABASES"]`** gets no help from the guard. With
  `USE_LOCAL_SET=True`, `async_tenant_context(using=...)` on such an alias raises
  `RLSConfigurationError` unless a transaction is open on the hop's thread (code driven by
  `async_to_sync` from inside `transaction.atomic(using=...)`). Add the alias to the list.
- **A procedure or `DO` block that commits** cannot run inside a context in autocommit in
  this mode; it fails with `invalid transaction termination` (see
  [the troubleshooting entry](../troubleshooting.md#a-procedure-that-commits-fails-with-invalid-transaction-termination)).

## Testing the async path

- Django's `AsyncClient` sends requests through the middleware's async path (`__acall__`).
  It does not reproduce the server's thread and connection lifecycle: there is no
  per-request thread, so the hops run on whichever thread the test's event loop sends them
  to (the test thread under Django's `TestCase`, asgiref's shared executor thread under
  pytest-asyncio or anyio), and the `request_finished` handlers run on a worker thread with
  Django's connection cleanup disconnected.
- `async def` test methods on a Django `TestCase` run their hops on the test thread, in the
  test's transaction. pytest-asyncio and anyio tests use a separate connection; read
  [Which connection an async test uses](testing.md#which-connection-an-async-test-uses)
  before choosing.
- Run at least one suite as a role without `SUPERUSER` and `BYPASSRLS`. A superuser bypasses
  RLS, so isolation tests pass for the wrong reason.

## Deployment checklist

- [ ] The ASGI server runs `myproject.asgi:application` on an asyncio event loop.
- [ ] `RLSTenantMiddleware` comes after `AuthenticationMiddleware`, and you know which path
      serves your requests (the `django.request` `DEBUG` log above).
- [ ] `CONN_MAX_AGE = 0`. Connection reuse comes from Django's pool with
      `"reset": reset_rls_gucs`, or from PgBouncer.
- [ ] Behind PgBouncer in transaction mode: `USE_LOCAL_SET=True` and
      `DISABLE_SERVER_SIDE_CURSORS = True`.
- [ ] `RLS_TENANTS["DATABASES"]` lists every alias the application queries.
- [ ] The pool's `max_size` covers one process's concurrent requests plus its executor
      threads, and `max_connections` covers that for every worker process. Without a pool,
      the server caps the requests in flight.
- [ ] With `USE_LOCAL_SET=True`, multi-statement writes run in `transaction.atomic()` inside a
      synchronous function.
- [ ] Async code uses `async_tenant_context()` / `async_admin_context()`, and
      `@with_rls_context` sits directly on the `async def`.
- [ ] No database work goes to `thread_sensitive=False`, `run_in_executor()` or a bare thread
      pool unless it opens its own context.
- [ ] Every alias connects as a role without `SUPERUSER` or `BYPASSRLS`. `W005` (`E001` with
      `DEBUG = False`) checks every alias in `RLS_TENANTS["DATABASES"]`.
- [ ] `python manage.py check` shows no `W004`, `W005`, `E001`, `W010`, `W011` or `W013`.

## See also

- [Middleware: Running under ASGI](middleware.md#running-under-asgi)
- [Context Managers: Async Code](context-managers.md#async-code)
- [Connection Pooling](connection-pooling.md)
- [GUC Affinity Guard](../advanced/architecture.md#guc-affinity-guard)
- [Security Model: Connection-Level State](../advanced/security.md#connection-level-state)
- [Testing: Async Tests](testing.md#async-tests)
- [Troubleshooting](../troubleshooting.md)
