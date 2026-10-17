# Connection Pooling

When using a connection pooler like **PgBouncer** or **pgpool-II**, or Django's own
connection pool, special care is needed to prevent GUC variables from leaking between
requests through pooled connections. Each pooler has its own section:
[PgBouncer](#pgbouncer), [pgpool-II](#pgpool-ii) and
[Django's Connection Pool](#djangos-connection-pool). If you suspect a leak, start with
[Troubleshooting GUC Leaks](#troubleshooting-guc-leaks). For which combinations are safe
under ASGI, see [Async Deployment](async.md#connections-and-pooling).

## The Problem

By default, django-rls-tenants uses `set_config()` to set session-scoped GUC variables.
These persist for the lifetime of the PostgreSQL session. With a connection pooler
in **transaction mode**, multiple Django requests may share the same PostgreSQL session,
and GUC values from one request could leak to the next.

## The Solution: USE_LOCAL_SET

Enable `USE_LOCAL_SET` to use `SET LOCAL` instead of `set_config()`:

```python title="settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "myproject",
        "HOST": "pgbouncer-host",
        "PORT": "6432",
        "OPTIONS": {
            # PgBouncer connection
        },
    }
}

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": True,
}
```

### How SET LOCAL Differs

| Behavior | `set_config()` (default) | `SET LOCAL` |
|----------|------------------------|-------------|
| **Scope** | Session (connection) | Transaction |
| **Cleared when** | Explicitly by middleware | Automatically at `COMMIT`/`ROLLBACK` |
| **Connection pooling safe** | Only with cleanup | Yes |
| **Transaction per request** | No (autocommit around the view) | Synchronous path (WSGI): yes, opened by the middleware. Async path (ASGI): no (see below) |
| **Outside a transaction** | Written once, kept on the session | Each statement run under a context gets a transaction of its own (*1.4.0+*) |
| **Nesting support** | Full save/restore | The enclosing context's values are applied again; no write on entry (*1.5.0+*) |

Context managers restore the previous values when each block exits in both modes.
With `USE_LOCAL_SET=True` the library does not write them with `SET LOCAL` itself on an
alias in `RLS_TENANTS["DATABASES"]` (*changed in 1.5.0*): the GUC affinity guard applies
the context's values transaction-locally, and transaction end discards them. See
[Context Managers](context-managers.md#exceptions-inside-a-context-block) for how
restoration works after a failed statement or savepoint rollback.

### Where the Transaction Comes From

`SET LOCAL` only works inside a transaction. What supplies it depends on how the code
runs.

**WSGI requests.** Django's `ATOMIC_REQUESTS` is **not** the answer: it wraps only the view
callable, and the middleware sets the GUCs before the view runs. So when
`USE_LOCAL_SET=True`, `RLSTenantMiddleware` opens `transaction.atomic()` itself, on every
alias in `RLS_TENANTS["DATABASES"]`, around the rest of the middleware chain and the view.
The GUCs are in force for the whole request and disappear at the commit or rollback that
ends it. Since 1.5.0 the middleware writes nothing in that transaction: the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) sends one
`set_config()` statement before the view's first query (a request with one query runs
`BEGIN`, `set_config()`, the query, `COMMIT`; before, `BEGIN`, two `SET LOCAL` statements,
the query, `COMMIT`). Unauthenticated requests set no GUCs and get no transaction.

`ATOMIC_REQUESTS=True` remains optional. If you enable it as well, Django's per-view
transaction simply nests as a savepoint inside the middleware's.

**The middleware's async path** (ASGI, when every middleware listed after it can run
asynchronously; *changed in 1.4.0*). It cannot hold a synchronous `transaction.atomic()`
open across the awaited chain, so it opens no transaction and writes no GUC. The
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) gives each statement
the request's GUCs instead (see
[Code Outside a Request Transaction](#code-outside-a-request-transaction)), or once per
transaction for a sync view under `ATOMIC_REQUESTS` (Django does not support
`ATOMIC_REQUESTS` for async views). **A WSGI request is atomic in this mode and a request
on the async path is not**: there, a view that writes twice can commit the first write and
fail on the second. Wrap the view's writes in `transaction.atomic()` where that matters.
If a synchronous-only middleware is listed after `RLSTenantMiddleware`, Django runs it
synchronously even under ASGI, and the request transaction applies as under WSGI. Before
1.4.0 the middleware raised `RLSConfigurationError` for this mode under ASGI.

!!! note "Consequences of a transaction per request (WSGI)"
    As with `ATOMIC_REQUESTS`, catch database
    errors inside your own `transaction.atomic()` block or the rest of the request fails
    with "current transaction is aborted"; `transaction.on_commit()` callbacks run when
    the request ends; and `@non_atomic_requests` has no effect, because the middleware's
    transaction encloses the view anyway. A streaming response iterates its body after
    the transaction has ended, so queries made while streaming run without tenant
    context (fail-closed: no rows) -- establish a context inside the generator.
    Place middleware implementing `process_exception` before `RLSTenantMiddleware`,
    so handled view exceptions cannot bypass rollback; unsafe ordering raises
    `RLSConfigurationError`. Error handling runs inside the transaction too, and writes
    by error handlers are rolled back along with the failed view's writes. On the async
    path there is no request transaction, so none of this applies (see
    [Middleware](middleware.md#use_local_set-and-asgi)).

### Code Outside a Request Transaction

*Changed in 1.4.0.* Management commands, Celery tasks, scripts and requests on the
middleware's async path have no transaction that the middleware opened. Before 1.4.0 a
context manager entered without `transaction.atomic()` raised `RuntimeError`, so you had
to open one yourself. Now the
context managers write nothing there: they set the Python-side context, and the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) writes the GUCs
transaction-locally (`set_config(name, value, true)`) on every alias in
`RLS_TENANTS["DATABASES"]`:

- **Inside a transaction** (`transaction.atomic()`, the WSGI request transaction,
  `AUTOCOMMIT=False`, a transaction you opened with SQL `BEGIN`), the guard sets both
  GUCs in one statement before the first statement that needs them, and again when the
  context changes in the same transaction (sibling asyncio tasks that share the
  connection, say). The values end with the transaction, committed or rolled back.
- **In autocommit**, a statement run while a context is active gets a transaction of its
  own: `BEGIN`, the `set_config()` statement, your statement, `COMMIT`. That is three
  extra round trips per statement. The statement keeps its autocommit semantics: it
  commits on its own, a failing one is rolled back alone, and a failing `COMMIT` (a
  deferred constraint) raises from that statement. An `executemany()` runs in one such
  transaction, so it commits or fails as a whole (in plain autocommit, psycopg2 keeps the
  rows before a failing one).
- **Outside any context**, a statement gets no transaction of the guard's. If the
  session's own values are unknown (see "After a context ends" below), the next such
  statement, in autocommit or in a transaction, first clears them for the session with one
  extra `set_config()` statement; inside a transaction the clear sticks once it commits.

```python
# No transaction.atomic() needed: each statement gets a transaction of its own.
with tenant_context(tenant_id=42):
    orders = list(Order.objects.all())
```

Where you enter the context decides what it costs:

| The context is entered | Cost |
|---|---|
| Outside any transaction | Nothing on entry or exit. Each autocommit statement run under it costs three extra round trips: `BEGIN`, a `set_config()` statement, `COMMIT`. |
| Outside any transaction, with a `transaction.atomic()` inside the block | One `set_config()` statement per transaction, before its first statement that needs the GUCs, plus one per context change in it. |
| Inside `transaction.atomic()` (a `TestCase`, the WSGI request transaction, `ATOMIC_REQUESTS`) | *Changed in 1.5.0.* One `set_config()` statement before the block's first statement, and none while the same values stay in force. If the block exits with the transaction still open, one more `set_config()` puts the enclosing context's values back or clears them. Before 1.5.0 the block wrote the GUCs itself: two `current_setting()` reads and two `SET LOCAL` statements on entry, two on exit. After an earlier context has ended on that connection the guard first clears the session's values, one more statement, and a loop that enters `atomic()` and a context on every iteration pays it every time. |

Putting a batch of statements in one transaction is still worth it for performance: either
placement beats three extra round trips per statement once the batch runs more than a few,
and the writes commit together.

```python
from django.db import transaction

# The context outside: one set_config() for the whole transaction.
with tenant_context(tenant_id=42):
    with transaction.atomic():
        orders = list(Order.objects.all())

# Or atomic() outside: one set_config() before the block's first statement and one
# when it exits (plus one clear first if an earlier context has ended on this
# connection).
with transaction.atomic():
    with tenant_context(tenant_id=42):
        orders = list(Order.objects.all())
```

What to know about the wrapped statements:

- The guard sends `BEGIN`, `COMMIT` and `ROLLBACK` past Django's cursor, so no execute
  wrapper sees them. It logs them the way Django logs its own transaction control, though:
  they appear in `connection.queries` (`assertNumQueries` and `CaptureQueriesContext`
  count them) and on the `django.db.backends` logger. A wrapped statement counts as
  **four** queries, listed in this order: `BEGIN`, the `set_config()` statement, `COMMIT`,
  then the statement itself, because Django logs it when it finishes.
- Statements PostgreSQL may refuse inside a transaction block run as they are, unwrapped
  and without the context: `VACUUM` (also `VACUUM (ANALYZE) t`), `CLUSTER`, `REINDEX`,
  `DISCARD`; `CREATE` / `ALTER` / `DROP` of a `DATABASE`, `TABLESPACE` or `SUBSCRIPTION`,
  and `ALTER SYSTEM`; `CREATE [UNIQUE] INDEX CONCURRENTLY` and `DROP INDEX CONCURRENTLY`;
  and `ALTER TABLE ... DETACH PARTITION ... CONCURRENTLY`. The guard matches them on their
  grammar, from the statement's first words, so a statement that only mentions those words
  elsewhere (in a comment, say) is wrapped. None of them evaluates an RLS policy.
  `REFRESH MATERIALIZED VIEW CONCURRENTLY` is wrapped. `SET TRANSACTION` is transaction
  control, in both GUC modes: the guard writes nothing before it, so an `atomic()` entered
  inside a context can start with it, and writes the context's values after it.
- A procedure or a `DO` block that commits or rolls back (`CALL` of a procedure with
  `COMMIT` inside, say) cannot run inside a context in autocommit: it fails with "invalid
  transaction termination". Run it outside any context.
- If the guard's `ROLLBACK` after a failed statement cannot be confirmed (a broken
  connection, a second failure, an interruption), the guard closes the connection, which
  Django believed to be in autocommit. The next statement reconnects, a pool rolls back a
  connection it gets back, and the statement's own error is the one that propagates.
- The guard covers only the aliases in `RLS_TENANTS["DATABASES"]`. A context manager
  entered outside `transaction.atomic()` on any other alias still raises, as before: add the
  alias to the list. Inside `atomic()` on such an alias the managers still write `SET LOCAL`.
- It covers only the admin and tenant GUCs. Bypass flags set with `bypass_flag()`,
  `set_bypass_flag()` or `rls_context()` keep their explicit `is_local` parameter
  (default `False`).
- A statement run in autocommit inside a context gets the context's values in its own
  transaction, which override writes of the admin or tenant GUC that other code made for
  the session (`set_guc()`, `rls_context()` without `is_local=True`). Narrowing an
  `admin_context()` with `rls_context({"rls.is_admin": "false", ...})` has no effect there,
  though it does inside `transaction.atomic()` and in session mode. Change the context with `tenant_context()` /
  `admin_context()`.
- SQL that bypasses Django's cursor (a driver cursor, `callproc()`, the `copy` methods, the
  `FETCH` of an open server-side cursor) is not seen. Outside a transaction it sees no
  context values in this mode, since nothing is left on the session. Since 1.5.0 the same
  holds inside a block in `transaction.atomic()` until the block's first statement through
  Django's cursor: it sees the enclosing context's values, or none. After the block it sees
  the block's values no longer: the exit settle replaces or clears them.
- The guard is installed on connections that were already open when the app's `ready()`
  ran (opened in an earlier app's `ready()`, say), so a context applies on them too.

#### After a context ends

Code inside a context can write the admin or tenant GUC for the session (`set_guc()`,
`rls_context()` without `is_local=True`, a function, a `DO` block). In session mode the
context's exit restores the GUCs. In this mode nothing would, and the value would outlive
the context on a persistent or pooled connection. So when a context ends (a block exits, a
`for_user()` call's scope ends, a request served by `RLSTenantMiddleware` ends, the
`request_finished` safety net runs), the values that the sessions of the thread's
configured connections hold for themselves count as unknown. So do the session's values on
any connection after a statement that ran under a context's transaction-local values: that
reaches the connection of a `thread_sensitive=False` hop or a worker thread, which no block
exit on the request thread reaches. No statement runs when a session is marked. Before the
next statement run outside any context, in autocommit or in a transaction, the guard clears
the session's values with one extra `set_config()` statement: for example the next
request's session lookup on a persistent connection. That clear is for the session, so
once its transaction commits it sticks. Inside a transaction the mark itself takes effect
at the commit; a rollback undoes such a write anyway.

## PgBouncer

PgBouncer gives a client a server connection for its whole session, for one transaction or
for one statement, depending on `pool_mode`:

| `pool_mode` | `USE_LOCAL_SET=False` (default) | `USE_LOCAL_SET=True` |
|---|---|---|
| `transaction` | **Unsafe.** The GUCs stay on the server connection, which PgBouncer hands to another client after every transaction. | **Safe**, with the settings below. |
| `session` | **Safe.** | **Safe.** |
| `statement` | **Not supported.** PgBouncer refuses transaction blocks, so `transaction.atomic()` fails, and the GUCs leak as in transaction mode. | **Not supported.** A statement run under a context needs a transaction (see [Code Outside a Request Transaction](#code-outside-a-request-transaction)), and PgBouncer closes the connection: `transaction blocks not allowed in statement pooling mode`. |

The library cannot tell that it is talking to PgBouncer: the checkout clear and `W010` cover
Django's own pool only, and `W004` looks at `CONN_MAX_AGE`. No check warns about
`USE_LOCAL_SET=False` behind transaction pooling.

The configurations in this section and in [pgpool-II](#pgpool-ii) were checked against
PgBouncer 1.26 and pgpool-II 4.7 with PostgreSQL 16, Django 6.1, psycopg2 and psycopg 3.

### Transaction Mode (recommended)

```ini title="pgbouncer.ini"
[databases]
myproject = host=db.internal port=5432 dbname=myproject

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
pool_mode = transaction
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
max_client_conn = 1000
default_pool_size = 20

; Clear the RLS GUCs on each server connection after every transaction
; (see "Clearing the GUCs After Every Transaction" below).
server_reset_query = SELECT set_config('rls.is_admin', '', false), set_config('rls.current_tenant', '', false)
server_reset_query_always = 1
```

```text title="userlist.txt"
"app" "SCRAM-SHA-256$4096:..."
```

`auth_file` lists each role PgBouncer accepts, with its password or, as here, its SCRAM
secret: the `rolpassword` that `SELECT rolpassword FROM pg_authid WHERE rolname = 'app';`
returns (a superuser query). Connect as the application's plain role, not as a superuser or
a role with `BYPASSRLS` (see [W005](../troubleshooting.md#w005-the-connection-role-is-a-superuser-or-has-bypassrls)).
For `auth_query` and the other ways to authenticate, see PgBouncer's
[configuration reference](https://www.pgbouncer.org/config.html).

```python title="settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "myproject",  # the name in PgBouncer's [databases] section
        "USER": "app",
        "PASSWORD": "...",
        "HOST": "pgbouncer.internal",
        "PORT": "6432",
        "DISABLE_SERVER_SIDE_CURSORS": True,
    }
}

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": True,
}
```

With `pool_mode = transaction` and `USE_LOCAL_SET = True`:

- Every statement gets its GUCs inside the transaction it runs in: the middleware's request
  transaction under WSGI, your `transaction.atomic()`, or, for requests on the middleware's
  async path and code outside a transaction, a transaction of its own that the GUC affinity
  guard opens (see
  [Code Outside a Request Transaction](#code-outside-a-request-transaction)). That is what
  transaction pooling needs. *Changed in 1.4.0:* before, only WSGI requests and code
  inside `atomic()` worked in this mode.
- The library's values end with their transaction, so it leaves nothing on the server
  connection for the next client. That does not make the server connection clean: see
  [One Session-Level Write Reaches Every Later Client](#one-session-level-write-reaches-every-later-client).
- Pass `is_local=True` to `bypass_flag()`, `set_bypass_flag()` and `rls_context()` (inside
  `transaction.atomic()`): they are session-scoped by default in either mode, and nothing
  clears a flag that PgBouncer hands to another client.
- Set `DISABLE_SERVER_SIDE_CURSORS = True` on the alias. Django requires it for transaction
  pooling: a server-side cursor (`iterator()`) can outlive its transaction, and the next
  transaction may run on another server connection.
- Django connects with PgBouncer's default (empty) `ignore_startup_parameters`, with either
  driver.

#### Why Session GUCs Leak Here

With `USE_LOCAL_SET=False`, the middleware and the context managers write the GUCs for the
session (`set_config(..., false)`). In transaction mode every statement Django runs in
autocommit is a transaction of its own, so PgBouncer can give the server connection to
another client right after the GUCs were written, and that client runs with them. No crash
or skipped cleanup is needed: a client with no context sees the first client's tenant while
the first client's context is still open, and a client inside `tenant_context(1)` whose
query lands on a server connection where another client has just written tenant 2 reads
tenant 2's rows. Under load it happens constantly; on a quiet system it looks
intermittent.

#### One Session-Level Write Reaches Every Later Client

With `USE_LOCAL_SET=True` the library writes the tenant and admin GUCs only
transaction-locally, and it assumes that a new connection's session holds none of them.
Behind PgBouncer that is true only while nothing else writes them for the session through
the same pool. A session-level write stays on its server connection until PgBouncer closes
that connection (once it has been idle for `server_idle_timeout`, ten minutes by default,
or, when idle, once it is older than `server_lifetime`, one hour; a busy pool rarely lets a
connection idle that long), and every client that gets the connection runs with the value
whenever it has no context of its own. A leaked
`extra_bypass_flags` entry applies even inside a tenant context, for the models that
declare it. Typical sources:

- `bypass_flag()`, `set_bypass_flag()` or `rls_context()` without `is_local=True`, and
  `set_guc()`;
- a client of the same PgBouncer database that runs with `USE_LOCAL_SET=False`: another
  deployment, a Celery worker or a management command with other settings;
- a `psql` session, migration or maintenance script run through PgBouncer that runs
  `SET rls.is_admin = 'true'` (without `LOCAL`) or `set_config(..., false)`;
- a function or `DO` block that calls `set_config(..., false)`;
- a per-database `connect_query` in `pgbouncer.ini` that sets a policy GUC.

The guard's clear after a context ends (see [After a context ends](#after-a-context-ends))
does not help here: it runs before the client's next statement, and PgBouncer may send that
statement to another server connection than the one that holds the value.

#### Clearing the GUCs After Every Transaction

`server_reset_query` is the query PgBouncer runs on a server connection when a client
releases it, before another client gets it. By default (`DISCARD ALL`, with
`server_reset_query_always = 0`) it runs only in session pools. With
`server_reset_query_always = 1` it also runs in transaction pools, after every transaction.
A reset query that clears the RLS GUCs, as in the example above, removes a session-level
write as soon as the transaction that made it ends:

- `USE_LOCAL_SET=True` works as before. The library's own values never outlive their
  transaction, so the reset finds nothing of theirs to remove.
- `USE_LOCAL_SET=False` stops working, fail-closed: the GUCs the middleware writes are gone
  before the view's first query, so the view's queries return zero rows (only a context
  entered inside `transaction.atomic()` still works, since its write and its queries share
  the transaction). A deployment with the wrong setting shows at once instead of leaking.
- A bypass flag set without `is_local=True` does not reach the next transaction. Set it with
  `is_local=True` inside `transaction.atomic()`, as transaction mode requires anyway.
- Each transaction costs one more statement between PgBouncer and PostgreSQL. With
  `USE_LOCAL_SET=True` outside `transaction.atomic()`, each statement run under a context is
  a transaction of its own, so that is one per statement.
- List every GUC your policies read: the tenant and admin GUCs (`rls.current_tenant` and
  `rls.is_admin` with the default `GUC_PREFIX`), each `extra_bypass_flags` entry, and the
  GUC names of any constraint, `AddRLSPolicy` or hand-written policy that uses names of its
  own (see [What the Library Does on Checkout](#what-the-library-does-on-checkout)).

#### Prepared Statements (psycopg 3)

Django's psycopg 3 backend binds parameters on the client and turns psycopg's automatic
prepared statements off (`prepare_threshold=None`) unless you set `OPTIONS`, and psycopg2
never prepares statements, so nothing needs configuring. With
`OPTIONS = {"server_side_binding": True, "prepare_threshold": 5}` (or another threshold),
psycopg 3 prepares a statement on its connection once it has run that often. Behind
transaction pooling the next transaction can run on another server connection, so PgBouncer
must track prepared statements: that needs PgBouncer 1.21 or later with
`max_prepared_statements` above zero (the default is 200 since 1.24). With
`max_prepared_statements = 0`, queries fail with errors such as
`prepared statement "_pg3_0" already exists`. Server-side binding without a
`prepare_threshold` prepares nothing and needs no PgBouncer setting.

### Session Mode

With `pool_mode = session`, a client keeps one server connection until it disconnects.
PgBouncer then runs `server_reset_query` (`DISCARD ALL` by default) and hands the connection
to the next client clean. Both GUC modes work:

- With `CONN_MAX_AGE = 0` (Django's default), Django disconnects at the end of every
  request, so every request starts on a reset server connection. With
  `USE_LOCAL_SET=False` the middleware also clears the GUCs at the end of the request.
- With `CONN_MAX_AGE > 0`, Django keeps its connection to PgBouncer, and with it the server
  connection, from one request to the next, and PgBouncer resets nothing in between. That
  is a persistent connection: the middleware clears the GUCs at the end of each request,
  the `request_finished` safety net clears them when that cleanup did not run (and logs a
  `gucs.cleared` [audit record](audit-logging.md)), and `W004` warns about it with
  `USE_LOCAL_SET=False` (see
  [Django's Persistent Connections](#djangos-persistent-connections-conn_max_age)).
- `DISCARD ALL` resets every setting to the session's default. A default for a policy GUC
  set with `ALTER ROLE ... SET` or `ALTER DATABASE ... SET` comes back instead of being
  cleared (`W011`).
- PgBouncer needs one server connection per connected client in this mode: it saves the
  cost of opening connections, not connections.

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": False,  # default, works with session mode
}
```

## pgpool-II

pgpool-II pools per session. One pgpool child process serves a client for its whole session
and holds the backend connections for it. When the client disconnects, the child runs
`reset_query_list` on every backend and, with `connection_cache = on`, keeps the connections
for its next client with the same user and database. There is no transaction pooling, so a
server session is never handed to another client mid-session, and without load balancing
both GUC modes work as they do on a direct connection.

The settings that matter here (backends, authentication, health checks and failover are
configured as usual; see the
[pgpool-II documentation](https://www.pgpool.net/docs/latest/en/html/)):

```ini title="pgpool.conf"
backend_clustering_mode = 'streaming_replication'
backend_hostname0 = 'db-primary'
backend_port0 = 5432
backend_hostname1 = 'db-standby'
backend_port1 = 5432

# Defaults, shown for reference.
connection_cache = on
reset_query_list = 'ABORT; DISCARD ALL'
num_init_children = 32
max_pool = 4

# The default is on; see "Load Balancing" below.
load_balance_mode = off
```

- Keep `DISCARD ALL` in `reset_query_list`: it is what clears the session GUCs between
  clients. As in PgBouncer's session mode, it brings back a role or database default for a
  policy GUC (`W011`).
- With `CONN_MAX_AGE > 0`, Django does not disconnect between requests, and the session is
  a persistent connection, as in [Session Mode](#session-mode): the middleware's cleanup,
  the safety net and `W004` apply.
- `num_init_children` is the number of clients pgpool serves at once. Every Django thread
  with an open connection is one client; a client beyond the limit waits for a free child.

### Load Balancing

In streaming replication mode with `load_balance_mode = on`, pgpool sends a session's
read-only queries to a load-balancing node, a standby here, and everything else to the
primary. Each backend has its own session, with its own GUCs, and pgpool decides for each
statement where it goes:

- `SELECT set_config(...)`, the statement the library sets the GUCs with, calls a volatile
  function. With neither `write_function_list` nor `read_only_function_list` set, pgpool
  treats it as a write and sends it to the primary only.
- A plain `SELECT` goes to the standby, unless it runs in an explicit transaction after a
  write: `disable_load_balance_on_write = 'transaction'` (the default) keeps the rest of
  that transaction on the primary.

What that does to the two GUC modes:

| pgpool settings | `USE_LOCAL_SET=False` | `USE_LOCAL_SET=True` |
|---|---|---|
| `load_balance_mode = off` | **Works.** | **Works.** |
| `load_balance_mode = on` | **Broken, fail-closed.** Reads in autocommit, and reads in a transaction before its first write, run on the standby, which never got the GUCs: zero rows. Writes run on the primary, with the GUCs. | **Works, without load balancing.** The guard opens each transaction run under a context with its `set_config()` statement, which goes to the primary, and the rest of the transaction stays there. Only statements run outside any context reach the standby. |
| `load_balance_mode = on` and `disable_load_balance_on_write = 'off'` | **Broken, fail-closed**, as above. | **Broken, fail-closed.** Reads run on the standby without the GUCs: zero rows. |

Do not list `set_config` in `read_only_function_list`: pgpool then sends the `set_config()`
statement to the standby, and writes on the primary fail with
`new row violates row-level security policy`.

Turn load balancing off for the Django application. To read from a standby, give Django a
separate alias for it, route reads there with a
[database router](https://docs.djangoproject.com/en/stable/topics/db/multi-db/#automatic-database-routing),
and list the alias in `RLS_TENANTS["DATABASES"]`: the
[GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) then applies the
active context on that alias's connection too (see
[Multi-Database Support](context-managers.md#multi-database-support)).

## Django's Connection Pool

Django 5.1+ can pool connections itself: set `DATABASES[alias]["OPTIONS"]["pool"]` with
psycopg 3 (`pip install "psycopg[pool]"`). Closing a pooled connection, which Django does
at the end of each request, hands it back to a `psycopg_pool.ConnectionPool`. The pool
rolls back an open transaction and does nothing else, so session GUCs set with
`set_config()` (`USE_LOCAL_SET=False`, the default) stay on the connection for its next
borrower.

### The Problem

The middleware clears the GUCs before the connection goes back. It cannot when its cleanup
failed or reached a different connection: a view that closed its connection before the
response ended (a long-lived SSE stream, for one), a crash, a cancelled request. The
`request_finished` safety net does not help: it runs after Django's own
`close_old_connections` has already returned the connection to the pool. The next
borrower, such as an anonymous request or a Celery task, would then run with the previous
request's tenant or admin bypass and could read that tenant's rows. A leaked
`extra_bypass_flags` entry (for example `rls.is_login_request`) would do the same for the
models that declare it, even for a borrower that sets its own tenant context. Versions
before 1.4.0 did not clear a pooled connection on checkout (see
[Upgrading](../advanced/migration-guide.md#from-134-to-140)).

`W004` never fires for a working pool, because Django rejects a non-zero `CONN_MAX_AGE`
together with `OPTIONS["pool"]`.

### What the Library Does on Checkout

Django sends `connection_created` on every checkout from the pool. For an alias in
`RLS_TENANTS["DATABASES"]` whose `OPTIONS["pool"]` is enabled (`True` or a non-empty dict;
Django treats `{}`, `False` and `None` as no pool), the `connection_created` handler clears
every GUC the library's RLS policies read, before the new borrower's first query:

- It is one statement, so one round trip per checkout (plus a `COMMIT` under
  `AUTOCOMMIT=False`), about one per request on that alias.
- It runs with or without a request context, in both GUC modes, and even when the pool has
  a `reset` callback (see below).
- Then, with `USE_LOCAL_SET=False`, on the thread that set the request's GUCs, the handler
  applies the request's GUCs as before (a connection checked out mid-request). A pooled
  connection always starts clean. The
  [GUC affinity guard](../advanced/architecture.md#guc-affinity-guard) then applies the
  active context before each statement, on whichever thread runs with one (with
  `USE_LOCAL_SET=True`, transaction-locally), and tracks each checkout as a new session.
- If the clear fails (a statement timeout, a broken connection), the checkout fails
  closed: the handler logs a warning (`Failed to clear RLS GUCs on a connection for alias
  ... checked out of a connection pool; discarding it.`), closes the connection so the pool
  discards it instead of reusing it, and raises the error to the code that asked for the
  connection. The borrower's query fails; it never runs on a connection that may still be
  dirty.

"Every GUC the policies read" means the configured tenant and admin GUCs (derived from
`GUC_PREFIX`) and, from the constraints of every installed model, each `RLSConstraint`'s
`guc_tenant_var`, `guc_admin_var` and `extra_bypass_flags` entries, and each
`RLSM2MConstraint`'s `guc_tenant_var` and `guc_admin_var`. GUCs read only by hand-written
policies, by `AddM2MRLSPolicy` operations with non-default names, or only by an
`AddRLSPolicy` (its explicit GUC names and `extra_bypass_flags`, which live in the migration,
not on a model), are not cleared.
Names that are not custom GUC names, such as an `extra_bypass_flags` entry without a dot
(PostgreSQL only accepts dotted names for custom settings), are left out, so they cannot
fail the statement.

Only Django's own pool (`OPTIONS["pool"]`) is detected, and only on aliases listed in
`RLS_TENANTS["DATABASES"]`. List every alias you use with `tenant_context(using=...)`,
`admin_context(using=...)` or `bypass_flag(using=...)` too. A third-party pooling backend
that hands out used connections is not detected.

### Clearing on Return: `reset_rls_gucs`

The checkout clear protects the next borrower, but an idle connection still holds the last
borrower's values until then. A pool `reset` callback clears them as the connection goes
back. Django forwards every key of `OPTIONS["pool"]` to `psycopg_pool.ConnectionPool`:

```python title="settings.py"
from django_rls_tenants.pool import reset_rls_gucs

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        # ...
        "OPTIONS": {"pool": {"reset": reset_rls_gucs}},
    },
}
```

`psycopg_pool` calls it on the raw psycopg 3 connection, in one of its worker threads,
after rolling back any open transaction. `reset_rls_gucs` clears the same set of GUCs in one
statement and commits if the connection is not in autocommit, leaving it idle as the pool
requires. If it raises, the pool discards the connection instead of reusing it
(fail-closed). It costs one statement per return. With a `reset` callback, `psycopg_pool`
returns connections asynchronously through its worker, and a returned connection is not
available to waiting borrowers until the reset has run.

`django_rls_tenants.pool` imports nothing from the tenants layer at module level, so it is
safe to import from `settings.py` before Django is set up.

!!! note "Why not `DISCARD ALL` or `RESET ALL`"
    Django applies some session settings, such as the time zone, only once per pooled
    connection, through the pool's `configure=` callback; `init_connection_state` skips
    them for pooled connections. `RESET ALL` (which `DISCARD ALL` includes) would reset
    `TimeZone` to the session's default (the server, database or role setting), and
    nothing would set Django's again. `reset_rls_gucs`
    clears only the RLS GUCs. If you keep session GUCs of your own, write a `reset` that
    calls `reset_rls_gucs(conn)`, clears yours, and leaves the connection idle (commit
    unless it is in autocommit).

### With `USE_LOCAL_SET=True`

The GUCs set by the middleware, the tenant context managers and the GUC affinity guard
are transaction-local values, which end with the transaction, so a pooled connection never
holds them (*changed in 1.4.0:* also under ASGI and outside `transaction.atomic()`; before,
this was WSGI only and the middleware raised `RLSConfigurationError` under ASGI). Bypass
flags are not: `bypass_flag()`, `set_bypass_flag()` and `rls_context()` default to
`is_local=False` in either mode, so a flag whose block did not restore it can still go
back to the pool. System check `W010` does not fire in this mode; the checkout clear
still runs and covers those flags, and the next statement run outside any context after a
context has ended clears what the session may hold (see
[After a context ends](#after-a-context-ends)).

## System Checks

django-rls-tenants provides system checks for two pooling misconfigurations:

### W004: CONN_MAX_AGE with session GUCs

```
?: (django_rls_tenants.W004) DATABASES['default'] has CONN_MAX_AGE=600 with
USE_LOCAL_SET=False. This risks GUC state leaking between requests on persistent
connections. Consider USE_LOCAL_SET=True, which scopes the GUCs to transactions, so they
never stay on a connection.
	HINT: Set RLS_TENANTS['USE_LOCAL_SET'] = True, or set
DATABASES['default']['CONN_MAX_AGE'] = 0.
```

The alias and the configured `CONN_MAX_AGE` value (here `600`) are interpolated into the
message. It fires once for every alias in `RLS_TENANTS["DATABASES"]` with a non-zero or
`None` `CONN_MAX_AGE`.

**Fix:** Either:

- Set `USE_LOCAL_SET = True`, or
- Set `CONN_MAX_AGE = 0` (Django default, closes connections after each request).

### W010: Django's connection pool without a reset callback

```
?: (django_rls_tenants.W010) DATABASES['default'] uses Django's connection pool with
USE_LOCAL_SET=False and no pool 'reset' callback. A connection that a request's cleanup
did not reach goes back to the pool still holding that request's RLS GUCs, until its next
checkout clears them.
	HINT: Set DATABASES['default']['OPTIONS']['pool']['reset'] to
django_rls_tenants.pool.reset_rls_gucs, or set RLS_TENANTS['USE_LOCAL_SET'] = True.
```

It fires once for every alias in `RLS_TENANTS["DATABASES"]` whose `OPTIONS["pool"]` is
enabled without a `reset` (the pool is `True`, or a dict without `"reset"` or with
`"reset": None`), when `USE_LOCAL_SET=False`. Any `reset`, including your own, silences it.
The checkout clear (see above) protects the next borrower either way; the check asks for
the return-side clear so idle connections hold no tenant state.

**Fix:** Either:

- Set the pool's `reset` to `django_rls_tenants.pool.reset_rls_gucs` (see
  [Clearing on Return](#clearing-on-return-reset_rls_gucs)), or
- Set `USE_LOCAL_SET = True`. Session-scoped bypass flags can still go back to the pool in
  that mode; `reset_rls_gucs` clears them too.

!!! info "Retired checks"
    `W003` (1.3.1) and `W007` (1.3.2) warned about `USE_LOCAL_SET=True` without
    `ATOMIC_REQUESTS`. Since the middleware opens its own transaction, that combination
    is valid and the checks no longer exist. Check IDs are never reused.

## Django's Persistent Connections (CONN_MAX_AGE)

Django's built-in `CONN_MAX_AGE` setting controls how long database connections are
reused. With `USE_LOCAL_SET=False` and `CONN_MAX_AGE > 0`:

- The middleware clears GUCs in `process_response`.
- But if the response phase is skipped (e.g., due to an unhandled exception), GUCs
  could leak to the next request on the same connection.
- The `request_finished` signal handler provides a safety net for this case.

For maximum safety with persistent connections, enable `USE_LOCAL_SET`:

```python title="settings.py"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "CONN_MAX_AGE": 600,  # 10 minutes
    }
}

RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "USE_LOCAL_SET": True,
}
```

Under ASGI, keep `CONN_MAX_AGE = 0`: each request runs on a thread of its own, so a
persistent connection is never reused. Use Django's pool or PgBouncer instead (see
[Async Deployment](async.md#connections-and-pooling)).

## Troubleshooting GUC Leaks

**Symptoms.** Only behind the pooler, and more often under load:

- a request, task or command sees another tenant's rows, or every tenant's;
- an anonymous request, or code that runs without a context, sees rows at all (it should
  see none: [fail-closed](../advanced/security.md#fail-closed-design));
- a model with `extra_bypass_flags` shows every tenant's rows inside a tenant context.

The opposite, zero rows behind the pooler where the same code finds rows on a direct
connection, is the fail-closed side of the same problem: the GUCs were written on one
server connection and the query ran on another (pgpool-II's
[load balancing](#load-balancing), or `USE_LOCAL_SET=False` with the
[reset query](#clearing-the-gucs-after-every-transaction)).

### 1. Look at What a Server Connection Holds

Connect through the pooler, with the application's role and database, and run:

```sql
SELECT pg_backend_pid(),
       current_setting('rls.is_admin', true) AS is_admin,
       current_setting('rls.current_tenant', true) AS tenant;
```

Outside a context both columns must be empty (the GUC was set and cleared on this server
connection) or `NULL` (it never was). Any other value is a leak, held by the backend with
that process id. Add a column for each `extra_bypass_flags` entry, and use your own names
if you changed `GUC_PREFIX`.

PostgreSQL cannot show another session's settings (`pg_stat_activity` has no GUC values),
so the query has to run on the server connection you want to look at. In transaction mode
PgBouncer gives each transaction whichever server connection is free: run the query a few
times, or check a staging pool with `default_pool_size = 1`, which has only one.
PgBouncer's `SHOW SERVERS` (on its admin console) lists each server connection with its
backend's process id (`remote_pid`) and the time it connected. Behind pgpool-II with load
balancing on, the query can run on a standby; start it with a comment, such as
`/*NO LOAD BALANCE*/ SELECT ...`, to run it on the primary: with pgpool's default
`allow_sql_comments = off`, any leading comment keeps a query off the standby.

### 2. Check That the Pool Resets

In staging, with `default_pool_size = 1` (behind pgpool-II, `num_init_children = 1`) so
that both clients get the same server connection, write a GUC for the session from one
client and read it from another:

```console
$ psql "host=pgbouncer.internal port=6432 dbname=myproject user=app" \
    -c "SELECT set_config('rls.current_tenant', '1', false)"
$ psql "host=pgbouncer.internal port=6432 dbname=myproject user=app" \
    -c "SELECT current_setting('rls.current_tenant', true)"
```

In transaction mode without the [reset query](#clearing-the-gucs-after-every-transaction),
the second client prints `1`: the first client's write reached it. With the reset query, in
session mode, and behind pgpool-II with load balancing off, it prints an empty value.
Without the reset query, the test leaves the value on the server connection: run
`RECONNECT` afterwards (step 5).

### 3. Find the Session-Level Write

With `USE_LOCAL_SET=True` the library writes the tenant and admin GUCs only
transaction-locally (`set_config(..., true)`). The only session-level writes of them it
makes are clears to an empty value. In the PostgreSQL log, a statement that sets a policy
GUC to a non-empty value for the session, `set_config('rls....', '...', false)` or
`SET rls.... = ...` without `LOCAL`, is the source; the
[list of typical sources](#one-session-level-write-reaches-every-later-client) says where
it usually comes from. To log every statement of the application's role in staging, run
`ALTER ROLE app SET log_statement = 'all';` as a superuser. It applies to new sessions, so
run `RECONNECT` on PgBouncer too, and `ALTER ROLE app RESET log_statement;` when you are
done.

A `gucs.cleared` [audit record](audit-logging.md) means a request's cleanup did not run and
the `request_finished` safety net ended its context. With `USE_LOCAL_SET=False` (session
pools, pgpool-II) the safety net cleared the GUCs on every configured alias; with
`USE_LOCAL_SET=True` it wrote nothing and marked the sessions for the GUC affinity guard,
which clears them before their next statement.

### 4. Rule Out Defaults

`python manage.py check` reports a default for a policy GUC set with `ALTER ROLE ... SET`
or `ALTER DATABASE ... SET` as `W011` (see
[W011](../troubleshooting.md#w011-postgresql-applies-a-default-for-a-policy-guc)).
`DISCARD ALL` restores such a default instead of clearing it. The check reads
`pg_db_role_setting` only, so it does not see a value that PgBouncer's per-database
`connect_query` sets.

### 5. Clean Up

- **PgBouncer:** `RECONNECT` (every database) or `RECONNECT myproject` on the admin console
  closes each server connection once its client releases it, and the new ones start without
  the value. Restarting PgBouncer does the same. PgBouncer closes a server connection on
  its own once it has been idle for `server_idle_timeout` (ten minutes by default) or, when
  idle, once it is older than `server_lifetime` (one hour).
- **pgpool-II:** the next disconnect of the client runs `reset_query_list`. A client that
  keeps its connection (`CONN_MAX_AGE > 0`) keeps its session until Django closes it.
- **Django's pool:** every checkout clears the policy GUCs (see
  [What the Library Does on Checkout](#what-the-library-does-on-checkout)).

### 6. Prevent It

- [ ] PgBouncer in transaction mode only with `USE_LOCAL_SET=True`, for every client of that
      database: every deployment, worker and management command.
- [ ] A `server_reset_query` that clears every policy GUC, with
      `server_reset_query_always = 1`.
- [ ] Bypass flags with `is_local=True`, inside `transaction.atomic()`; no `set_guc()` or
      `rls_context()` without `is_local=True`.
- [ ] No PgBouncer statement mode.
- [ ] pgpool-II with `load_balance_mode = off`; replicas through their own Django alias,
      listed in `RLS_TENANTS["DATABASES"]`.
- [ ] A maintenance session that needs the admin bypass connects to PostgreSQL directly, or
      sets it with `SET LOCAL rls.is_admin = 'true'` inside `BEGIN` ... `COMMIT`.
- [ ] `python manage.py check` reports no `W004`, `W005`, `W010` or `W011`.
- [ ] A test that asserts the database filters, not only the ORM: the `enforce_rls`
      fixture or `assert_rls_enforced()` (see
      [Enforcing RLS in Tests](testing.md#enforcing-rls-in-tests)), run in CI as the
      role the application connects as.
