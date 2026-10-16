# Management Commands

## check_rls

The `check_rls` management command verifies that all `RLSProtectedModel` subclasses
have the expected RLS policies applied in the database.

### Usage

```bash
python manage.py check_rls
python manage.py check_rls --database replica  # check a specific database
python manage.py check_rls --quiet            # only print output on failure (for CI)
python manage.py check_rls --verbose          # show each policy's USING / WITH CHECK SQL
```

### Successful Output

```
  Order (myapp_order): myapp_order_tenant_isolation_policy
  Invoice (myapp_invoice): myapp_invoice_tenant_isolation_policy
  Document (myapp_document): myapp_document_tenant_isolation_policy

M2M through tables:
  Project.members (auto M2M) (myapp_project_members): myapp_project_members_m2m_rls_policy
  Project.tags (auto M2M) (myapp_project_tags): myapp_project_tags_m2m_rls_policy

All 5 RLS-protected tables verified.
```

### Failure Output

If any issues are found, the command prints errors and exits with status code 1:

```
  Order (myapp_order): myapp_order_tenant_isolation_policy

Found 2 issue(s):
  Invoice (myapp_invoice): RLS not enabled
  Document (myapp_document): no RLS policies found
```

If a catalogue query fails (for example `permission denied for view pg_policies`) or the
connection cannot be used, the command does not print a traceback and does not report
success. It writes this to stderr and exits with status code 1:

```
Could not determine the RLS status of the protected tables on database 'default': the catalogue query failed with ProgrammingError: permission denied for view pg_policies. Nothing was verified; fix the cause and run check_rls again.
```

Like every management command, `check_rls` runs Django's system checks first, so with
`DEBUG = False` and a superuser or `BYPASSRLS` connection role it stops at the
[`E001`](../troubleshooting.md#e001-the-connection-role-bypasses-rls-with-debug-off) error
before it checks anything.

### What It Checks

The command performs two batched queries against PostgreSQL system catalogs for both
standard RLS-protected tables and M2M through tables:

1. **`pg_class`**: verifies that `relrowsecurity` (RLS enabled) and `relforcerowsecurity`
   (RLS forced) are both `True` for each protected table.

2. **`pg_policies`**: verifies that at least one RLS policy exists for each protected table.

M2M through tables are auto-discovered by scanning `RLSProtectedModel` subclasses for
`ManyToManyField` definitions with auto-generated through tables.

### When to Run

- **After migrations**: to verify that RLS policies were created correctly.
- **In CI/CD**: as a post-migration check before deploying.
- **After manual schema changes**: to ensure nothing was accidentally dropped.

```yaml title=".github/workflows/ci.yml"
- name: Run migrations
  run: python manage.py migrate

- name: Verify RLS policies
  run: python manage.py check_rls --quiet
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--database` | `default` | Database alias to check RLS status on |
| `--quiet` | off | Suppress success output; only print errors (exit 1 on failure). Ideal for CI. |
| `--verbose` | off | Show each policy's full definition (USING / WITH CHECK SQL) from the `pg_policies` view. |

### Quiet Mode

`--quiet` silences all success output — the per-table listing, the M2M header, and
the final summary line. On a fully-correct database, stdout is empty. Errors are never
suppressed: when RLS is missing or misconfigured, the command still writes the error
report to stderr and exits with status code 1.

### Verbose Mode

`--verbose` prints the live policy definition from the `pg_policies` view (its `qual` /
`with_check` columns, which are PostgreSQL's own `pg_get_expr()` output) under each
pass/fail line — the SQL expression PostgreSQL actually enforces, not a reconstruction.
It complements the pass/fail check rather than replacing it: the exit code and error
reporting are unchanged. `--quiet` and `--verbose` are logical opposites; `--quiet`
takes precedence, so verbose detail only appears on a non-quiet run.

```
  Order (myapp_order): myapp_order_tenant_isolation_policy
    myapp_order_tenant_isolation_policy [ALL]
      USING:
        CASE
            WHEN (( SELECT current_setting('rls.is_admin'::text, true) AS current_setting) = 'true'::text) THEN true
            ELSE (tenant_id = (NULLIF(( SELECT current_setting('rls.current_tenant'::text, true) AS current_setting), ''::text))::integer)
        END
      WITH CHECK:
        CASE
            WHEN (( SELECT current_setting('rls.is_admin'::text, true) AS current_setting) = 'true'::text) THEN true
            ELSE (tenant_id = (NULLIF(( SELECT current_setting('rls.current_tenant'::text, true) AS current_setting), ''::text))::integer)
        END
```

### Limitations

- Does not verify the **content** of the RLS policy (e.g., correct GUC variable names).
  It only checks that a policy exists. Use `--verbose` to eyeball the policy body manually.
- Does not check policies on non-`RLSProtectedModel` tables. If you manually manage
  RLS policies, use `assert_rls_policy_exists()` from the testing helpers.

## setup_m2m_rls

The `setup_m2m_rls` management command discovers M2M through tables on RLS-protected
models and applies subquery-based RLS policies. Designed for **existing deployments**
that need to add M2M RLS coverage without re-running migrations.

### Usage

```bash
python manage.py setup_m2m_rls              # apply policies
python manage.py setup_m2m_rls --dry-run    # preview SQL without executing
python manage.py setup_m2m_rls --verbose    # print each policy's SQL, then apply it
python manage.py setup_m2m_rls --database replica
```

### How It Works

1. Discovers auto-generated M2M through tables on `RLSProtectedModel` subclasses.
2. Checks which tables already have RLS policies (via `pg_policies`).
3. For unprotected tables, generates and executes `EXISTS`-based subquery policies.
4. Skips tables that already have policies applied.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--database` | `default` | Database alias to apply policies on |
| `--dry-run` | off | Print SQL without executing |
| `--verbose` | off | Print each policy's SQL before applying it |

### When to Use

- **After upgrading** to a version with M2M RLS support, if your existing M2M tables
  don't have policies yet.
- **After adding M2M fields** to existing `RLSProtectedModel` subclasses without
  creating a migration with `AddM2MRLSPolicy`.

!!! tip
    For new M2M fields, prefer using `AddM2MRLSPolicy` in a migration instead.
    `setup_m2m_rls` is a convenience for retroactive application.

### Verbose Mode

`--verbose` prints the same `CREATE POLICY` SQL that `--dry-run` shows, but then still
applies each policy — useful for DBAs and security reviewers who want to audit the SQL
while applying it in one run. Passing both `--verbose` and `--dry-run` is equivalent to
`--dry-run`: the SQL is printed once and nothing is executed.

```
-- Project.members (auto M2M) (test_project_members):
CREATE POLICY ...

Project.members (auto M2M) (test_project_members): policy applied
```

## run_as_tenant

*Added in 1.5.0.*

`run_as_tenant` runs another management command inside one tenant's RLS context, so a
command written without any context handling (a nightly report, a re-index, a one-off
data fix, a backfill) sees exactly that tenant's rows. It looks the tenant up, enters
`tenant_context()` and calls the command with `call_command()`.

### Usage

```bash
python manage.py run_as_tenant --tenant 5 my_report
python manage.py run_as_tenant --tenant acme --lookup slug my_report --since 2026-01-01
python manage.py run_as_tenant --admin my_report            # admin_context(): every tenant's rows
python manage.py run_as_tenant --database replica --tenant 5 my_report
```

Everything after the command's name is passed on to that command, so
`run_as_tenant`'s own options must come **before** the name. An option written after it
(`my_report --tenant 5`) belongs to `my_report`. A `--` right after the name is dropped,
so `my_report -- --flag x` works for commands that need it.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--tenant VALUE` | | The tenant to run as, matched against the `--lookup` field. |
| `--admin` | off | Run inside `admin_context()` instead of a tenant. |
| `--lookup FIELD` | `pk` | Field of the tenant model that `--tenant` is matched against. Any field or ORM lookup works, for example `slug`. |
| `--database` | `default` | Database alias to enter the context on and to read the tenant from. See [Databases](#databases). |

Exactly one of `--tenant` and `--admin` is required.

The tenant model is `RLS_TENANTS["TENANT_MODEL"]`, read through its `_default_manager`
inside `admin_context()`, so a tenant model that is itself RLS-protected still resolves.
These are `CommandError`s, not tracebacks: an unknown tenant, a `--lookup` value that
matches more than one tenant, a value of the wrong type for the field (`--tenant abc` with
the default `pk`), an unknown `--lookup` field, an unknown command and an unknown
database alias. An error raised by the inner command propagates unchanged.

`-v 2` names what was resolved on stderr:

```
Running my_report in tenant Acme Corp (pk=5) on database 'default'.
```

### From code

`call_command()` works too. Pass the inner command's options as strings in the
arguments, not as keyword arguments, because they belong to the inner command:

```python
from django.core.management import call_command

call_command("run_as_tenant", "--tenant", "5", "my_report", "--since", "2026-01-01")
```

### STRICT_MODE

The tenant lookup runs in `admin_context()` and the inner command in the tenant's
context, so `STRICT_MODE=True` is satisfied. Running the same command directly, with no
context, raises `NoTenantContextError`, which is what you want from a command that has
no business running tenant-blind.

## run_for_all_tenants

*Added in 1.5.0.*

`run_for_all_tenants` runs another management command once per tenant, each run inside
that tenant's own `tenant_context()`, and reports every tenant's outcome. It is the
command to schedule for per-tenant reports, re-indexing and backfills.

### Usage

```bash
python manage.py run_for_all_tenants my_report
python manage.py run_for_all_tenants --filter is_active=True my_report --since 2026-01-01
python manage.py run_for_all_tenants --parallel 4 --json my_report
python manage.py run_for_all_tenants --fail-fast reindex_orders
```

As with `run_as_tenant`, everything after the command's name is passed on to it, so the
options below must come before the name.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--filter FIELD=VALUE` | all tenants | Only run for tenants matching this `QuerySet.filter()` lookup. Repeatable; all filters must match. |
| `--parallel N` | `1` | Run up to `N` tenants at once, in worker processes. See [Running in parallel](#running-in-parallel). |
| `--json` | off | Print one JSON object per tenant instead of the human-readable report. See [JSON output](#json-output). |
| `--fail-fast` | off | Stop at the first tenant that fails; the tenants not yet run are reported as skipped. |
| `--database` | `default` | Database alias to enter the contexts on and to read the tenants from. See [Databases](#databases). |

Tenants run in primary key order. A `--filter` value is a string handed to
`QuerySet.filter()`, so lookups work (`--filter name__startswith=A`) and a value must be
one Django can convert from a string. A `BooleanField` accepts `True`, `False`, `1` and
`0`, not lowercase `true`. A malformed filter or an unknown field is a `CommandError`.

The tenant list is read in `admin_context()`, with the `RLS_TENANTS["TENANT_MODEL"]`
model's `_default_manager`.

### Output

```
== Acme Corp (pk=1) ==
Notes: 5
  Work: 3
Acme Corp (pk=1): OK in 0.012s
== Globex Inc (pk=2) ==
Notes: 4
Globex Inc (pk=2): OK in 0.009s
note_report: 2 tenant(s) succeeded, 0 failed, 0 skipped.
```

Each tenant gets a `== <label> (pk=<pk>) ==` header, the command's own output, and a
status line. The label is `str(tenant)`. `-v 0` drops the headers, the status lines and
the summary and leaves only the command's output. If no tenant matches, the command
prints `No tenants matched; nothing to run.` and exits with status 0.

### Failures and exit status

A tenant fails when the command raises any exception, or exits with
`SystemExit` and a non-zero status (reported as `exited with status N`). That does not
stop the other tenants, unless you pass `--fail-fast`. The failure is written to stderr,
with the traceback if you pass `--traceback`:

```
Globex Inc (pk=2): FAILED in 0.004s: ValueError: no export bucket configured
note_report: 1 tenant(s) succeeded, 1 failed, 0 skipped.
```

After the summary the command raises a `CommandError`, so the process exits with status
1 and the failed tenants are named:

```
CommandError: note_report failed for 1 of 2 tenant(s): Globex Inc (pk=2)
```

With `--fail-fast` the tenants after the first failure are not run, and each is reported
as `<label> (pk=<pk>): skipped` and counted under "skipped".

### JSON output

With `--json`, stdout holds one JSON object per line and nothing else, one line per
tenant, as each finishes. There is no summary, and the final error goes to stderr, so the
output can be piped to `jq` or parsed by a scheduler:

```bash
python manage.py run_for_all_tenants --json my_report > report.ndjson
```

```json
{"tenant": 1, "label": "Acme Corp", "status": "ok", "seconds": 0.012, "error": null, "traceback": null, "stdout": "Notes: 5\n  Work: 3\n", "stderr": ""}
{"tenant": 2, "label": "Globex Inc", "status": "failed", "seconds": 0.004, "error": "ValueError: no export bucket configured", "traceback": "Traceback (most recent call last): ...", "stdout": "", "stderr": ""}
```

| Key | Value |
|-----|-------|
| `tenant` | The tenant's primary key. |
| `label` | `str(tenant)`. |
| `status` | `"ok"`, `"failed"` or `"skipped"`. |
| `seconds` | How long the command took; `null` for a skipped tenant. |
| `error` | `"<ExceptionType>: <message>"` or `"exited with status N"`; `null` unless failed. |
| `traceback` | The formatted traceback of a failure, always (not only with `--traceback`). |
| `stdout`, `stderr` | What the command wrote; `null` for a skipped tenant. |

`stdout` and `stderr` hold the output the command wrote through `self.stdout` and
`self.stderr`. A bare `print()` goes to the process's real stdout and is not captured
(under `--json` it would also break the one-object-per-line format), so use
`self.stdout.write()` in commands meant for this.

### Running in parallel

`--parallel N` runs up to `N` tenants at once in a pool of worker processes:

```bash
python manage.py run_for_all_tenants --parallel 4 --json my_report
```

- The workers are **spawned**, not forked: a forked child would share the parent's
  PostgreSQL sockets. Each worker runs `django.setup()` with the parent's
  `settings.SETTINGS_MODULE` and the parent's database names (so a test database
  reaches the workers) and opens its own connections. A spawned worker imports
  `manage.py` as a module, so it must keep the `if __name__ == "__main__":` guard
  Django's template gives it.
- It needs a settings module (`DJANGO_SETTINGS_MODULE` or `--settings`). Settings
  built with `settings.configure()` cannot reach the workers, and the command stops with a
  `CommandError`.
- The workers only see **committed** rows. Called inside an open transaction (a test
  under `django_db` without `transaction=True`, or `transaction.atomic()`), the
  uncommitted tenants and data are invisible to them. The parent's connections are
  not closed before the pool starts, so the caller's transaction is left alone.
- Results are reported as they finish, in completion order rather than primary key
  order. Each worker's output is captured and printed under its tenant's header, so
  tenants' output never interleaves.
- A worker process that dies (it is killed, or runs out of memory) fails the tenants it
  was running with `the worker process died: ...`. The pool is broken then, so the
  tenants that had not finished are reported as failed too.
- With `--fail-fast`, tenants not yet started are cancelled and reported as skipped;
  tenants already running finish.

Each worker pays Django's start-up cost, and `N` workers hold up to `N` database
connections, so size `N` to the database, not to the tenant count.

### Databases

`--database` (on both commands) names the alias to read the tenants from and to enter
the context on. It defaults to `default`. If the inner command declares its own
`--database` option, the alias is passed on to it as `--database=<alias>`, so a
command that takes `--database` works on the same database. It is only passed on when
you give `--database`, and only to a command that declares the option. It is placed before
your arguments, so an explicit `--database` written after the command's name wins:

```bash
python manage.py run_for_all_tenants --database replica my_report            # my_report --database=replica
python manage.py run_for_all_tenants --database replica my_report --database default
```

With `USE_LOCAL_SET=True`, an alias outside `RLS_TENANTS["DATABASES"]` is refused with a
`CommandError`: a context on it would need `transaction.atomic()` around the whole
command, and these commands do not open a transaction. Add the alias to
`RLS_TENANTS["DATABASES"]`. An alias that is not in `settings.DATABASES` is a
`CommandError` as well.

### Scheduling it

```yaml title=".github/workflows/nightly.yml"
- name: Per-tenant report
  run: python manage.py run_for_all_tenants --parallel 4 --json note_report > report.ndjson
```

```cron
0 2 * * *  cd /srv/app && python manage.py run_for_all_tenants -v 0 reindex_orders
```

The exit status is 1 when any tenant failed, so a scheduler or CI job reports the
failure without parsing the output.

!!! tip
    The inner command needs no RLS code at all: it uses the ORM as it would in a view
    and sees only the current tenant's rows. If it needs its own control flow (tenants
    that depend on one another, a shared transaction, per-tenant arguments), write the
    loop yourself with `tenant_context()` as shown below.

## Using RLS in Custom Management Commands

Management commands run outside the request/response cycle, so the middleware does not
set any RLS context.

For a command that only needs to run for one tenant or for each of them, do not add
context handling to it: run it through [`run_as_tenant`](#run_as_tenant) or
[`run_for_all_tenants`](#run_for_all_tenants). The command stays an ordinary Django
command that uses the ORM.

When the command needs its own logic, such as a tenant argument of its own or work that
spans tenants, set the context explicitly with the context managers:

```python title="myapp/management/commands/process_orders.py"
from django.core.management.base import BaseCommand
from django_rls_tenants import tenant_context, admin_context


class Command(BaseCommand):
    help = "Process pending orders"

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", type=int, help="Process a specific tenant")

    def handle(self, *args, **options):
        tenant_id = options.get("tenant_id")

        if tenant_id:
            # Process a specific tenant
            with tenant_context(tenant_id=tenant_id):
                self._process_orders()
        else:
            # Process all tenants (admin mode)
            with admin_context():
                tenants = Tenant.objects.all()
                for tenant in tenants:
                    with tenant_context(tenant_id=tenant.pk):
                        self._process_orders()

    def _process_orders(self):
        orders = Order.objects.filter(status="pending")
        for order in orders:
            order.process()
            self.stdout.write(f"  Processed: {order.title}")
```

!!! warning
    Without a context manager, queries against RLS-protected tables will return
    zero rows (fail-closed). This is intentional -- it prevents accidental
    cross-tenant data access in scripts.

!!! tip
    With `STRICT_MODE=True`, queries without a context manager raise
    `NoTenantContextError` instead of silently returning empty results. This is
    especially useful during development to catch management commands that forget
    to set a context.
