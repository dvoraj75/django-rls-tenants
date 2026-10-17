# Audit Logging

*1.6.0+.* The library logs an audit record each time an RLS context is entered or exited,
each time it refuses one, when it rejects a foreign key to another tenant's row, and when
its safety net has to clear a request's context. Compliance frameworks such as SOC 2 and
GDPR ask for this kind of trail of data-access boundaries.

The records go to the `django_rls_tenants.audit` logger, a child of `django_rls_tenants`.
There is no library setting: Django's [`LOGGING`](https://docs.djangoproject.com/en/stable/topics/logging/)
setting turns the trail on, filters it and sends it where you want it. Every field is
also a `LogRecord` attribute, so a JSON formatter writes the fields without parsing the
message.

## Enabling it

Route the logger to a handler at `INFO`:

```python title="settings.py"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "audit": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "django_rls_tenants.audit": {
            "handlers": ["audit"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
```

Example output (the message lists the fields that are set):

```
context.entered source=middleware tenant_id=42 admin=False user_id=7
context.exited source=middleware tenant_id=42 admin=False user_id=7
context.denied source=queryset reason=strict_mode model=billing.Invoice
```

Without such an entry, and unless the root logger (or `django_rls_tenants`) already allows
`INFO`, nothing is logged at `INFO`. The `WARNING` records (refusals, rejected
foreign keys, the safety net) behave like any library warning: if neither the logger nor an
ancestor has a handler, Python's last-resort handler prints them to `stderr`. To drop them,
set the logger's level to `ERROR`, or give it a `logging.NullHandler` and `"propagate": False`.

A project that already logs `django_rls_tenants` at `DEBUG` (see
[Debug Logging](middleware.md#debug-logging)) receives the audit records there too, since
the audit logger propagates to its parent. `"propagate": False` above keeps them apart.

## JSON output

The fields are record attributes (`extra=`), so a formatter that writes a record's extra
attributes, such as [python-json-logger](https://pypi.org/project/python-json-logger/),
needs no configuration beyond the formatter:

```python title="settings.py"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "audit": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "loggers": {
        "django_rls_tenants.audit": {
            "handlers": ["audit"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
```

```json
{"asctime": "2026-10-17 09:12:01,532", "levelname": "INFO", "name": "django_rls_tenants.audit", "message": "context.entered source=drf tenant_id=42 admin=False user_id=7 using=default", "rls_event": "context.entered", "rls_source": "drf", "rls_tenant_id": 42, "rls_admin": false, "rls_user_id": 7, "rls_using": "default"}
```

(python-json-logger 3.1 and later; older releases name the class
`pythonjsonlogger.jsonlogger.JsonFormatter`.)

## Record fields

Every record has these attributes:

| Attribute | Value |
|-----------|-------|
| `rls_event` | The event, see [Events](#events). |
| `rls_source` | The code path that logged it, see [Sources](#sources). |
| `rls_tenant_id` | The tenant of the context, as the code passed it (`int`, `str`, `UUID`); `None` for the admin context or when there is none. For `fk.cross_tenant_rejected`, the tenant of the row being validated. |
| `rls_admin` | `True` for the admin context, `False` for a tenant context, `None` where it does not apply. |
| `rls_user_id` | The user's primary key, `None` where the library does not know the user (a context manager called directly, a Celery task, model validation). |

And, depending on the event:

| Attribute | Events | Value |
|-----------|--------|-------|
| `rls_using` | `context.entered`, `context.exited`, except from `middleware` | The alias the block was entered on (`using=`). Every alias in `RLS_TENANTS["DATABASES"]` follows the context anyway. |
| `rls_reason` | `context.denied` | Why the context was refused, see [Events](#events). |
| `rls_model` | `context.denied` (`strict_mode`, `object_of_other_tenant`), `fk.cross_tenant_rejected` | The model label, `"app_label.ModelName"`. |
| `rls_fields` | `fk.cross_tenant_rejected` | The names of the foreign keys whose target belongs to another tenant. |
| `rls_databases` | `gucs.cleared` | The aliases cleared (`RLS_TENANTS["DATABASES"]`). |

The user is identified by primary key only. No username, email address or other personal
data is logged, so the trail stays pseudonymous; resolve a primary key in a system with
its own access controls. A field that the library does not have (a request id, the client
address, the URL) can be added with a
[logging filter](https://docs.python.org/3/library/logging.html#filter-objects) on the
handler.

## Events

| Event | Level | Logged when |
|-------|-------|-------------|
| `context.entered` | `INFO` | A tenant or admin context is entered. |
| `context.exited` | `INFO` | That context ends, also when its block raises. One per `context.entered`, same fields. For a request whose middleware cleanup never ran, none is logged and the safety net's `gucs.cleared` comes instead; if the middleware's clear itself failed, `context.exited` is logged and `gucs.cleared` follows. |
| `context.denied` | `WARNING` | The library refuses to run without the right context (reasons below). |
| `fk.cross_tenant_rejected` | `WARNING` | Validation rejects a foreign key whose target row exists but belongs to another tenant, or to none (see [Cross-tenant foreign keys](protecting-models.md#cross-tenant-foreign-keys)). That includes a target Django's or DRF's own validation already rejected as not found because RLS or the auto-scope filter hides it (a tenant user sending another tenant's id): the library then reads the target's tenant once in `admin_context()`, only when this record is logged, and leaves the error as it was. A target that does not exist is an ordinary invalid value and is not logged. |
| `gucs.cleared` | `WARNING` | The `request_finished` safety net found a request's context still set (the middleware's own cleanup did not run, or its clear failed) and ran its clear, which skips a connection that is already closed (see [Connection-Level State](../advanced/security.md#connection-level-state)). If the clear raises, a warning goes to the `django_rls_tenants` logger instead. |

`context.denied` reasons:

| `rls_reason` | Source | Refusal |
|--------------|--------|---------|
| `user_without_tenant` | `middleware`, `drf`, `admin`, `decorator`, `for_user` | A user who is not a tenant admin has `rls_tenant_id=None` (`NoTenantContextError`, or `PermissionDenied` / a 403 in the admin and DRF; the admin only while `rls_deny_without_tenant` is `True`, the default). |
| `tenant_id_none` | the block's source | `tenant_context(None)` / `async_tenant_context(None)`. |
| `strict_mode` | `queryset` | `STRICT_MODE` refused a query with no context (`rls_model` names the model). |
| `no_context` | `drf`, `celery` | `RLSTenantPermission` found no context for an authenticated user (a view without `RLSTenantAPIViewMixin`), an `RLSTask` with `rls_require_context = True` arrived without one, or `CurrentTenantDefault` was called outside a tenant context (`rls_admin=True`: in the admin context). |
| `context_mismatch` | `drf` | `RLSTenantPermission.has_permission()`: the active context is not the user's (`rls_tenant_id` / `rls_admin` describe the active one). |
| `object_of_other_tenant` | `drf` | `RLSTenantPermission.has_object_permission()` refused an object of another tenant (`rls_model` names its model). |

## Sources

| `rls_source` | Code path | User known |
|--------------|-----------|------------|
| `middleware` | `RLSTenantMiddleware`, on every path (WSGI, ASGI, a subclass with its own hooks). The context ends with the request, before a streaming body is sent. | yes |
| `drf` | `RLSTenantAPIViewMixin` (the context `get_rls_context()` returned, an override's too), `RLSTenantPermission`, `RLSTenantSerializerMixin`, `CurrentTenantDefault` | yes (the serializer: when its `context` has the `request`) |
| `admin` | `RLSTenantModelAdmin`'s view wrappers | yes |
| `decorator` | `@with_rls_context`, sync and async | yes |
| `celery` | `RLSTask` and the `install()` signal handlers, on the worker | no |
| `command` | `run_as_tenant`, `run_for_all_tenants` (one entry per tenant; with `--parallel` the records come from the worker processes, so a file handler must be safe for several processes) | no |
| `context_manager` | `tenant_context()`, `admin_context()` and the async forms, called by your code (including blocks nested inside any of the above) | no |
| `queryset` | `STRICT_MODE` | no |
| `for_user` | `for_user()` with a tenantless user | yes |
| `validation` | `validate_cross_tenant_fks()` and `RLSProtectedModel.clean_fields()` (`full_clean()`, forms, the admin) | no |
| `safety_net` | the `request_finished` receiver | no |

A request served by the middleware logs one entry and one exit. A DRF view behind the
middleware adds its own pair only when the mixin enters a different context (a token
client, whom the middleware saw as anonymous, or an override that picks another tenant);
when the middleware's context already is the user's, the mixin enters nothing and logs
nothing.

## What is not logged

- **`for_user()` scopes.** A `for_user()` queryset sets its user's context for each SQL
  statement it runs, so a record per scope would flood the log. Only its refusal
  (`user_without_tenant`) is logged.
- **The library's own lookups.** Reading the tenant of a foreign key's target during
  validation, and the tenants `run_as_tenant` / `run_for_all_tenants` resolve or list,
  run in `admin_context()` blocks that are not logged. The context the command then runs
  in is.
- **Unauthenticated requests.** No context is set for them (fail-closed), and DRF answers
  an unauthenticated request 401 without a `context.denied` record.
- **GUCs written outside the context managers**, by `set_guc()`, `rls_context()`,
  `bypass_flag()` / `set_bypass_flag()` or raw SQL. These are lower-level tools; review
  their use in code (see [Recommendations](../advanced/security.md#recommendations)).
- **The GUC affinity guard re-applying a context** on another connection or thread. That is
  the same context, already logged when it was entered.
- **`@with_rls_context` called with `None` as the user.** It runs the function without a
  context (fail-closed) and logs a warning on the `django_rls_tenants` logger, as before.

The `DEBUG` messages on the `django_rls_tenants` logger (see
[Debug Logging](middleware.md#debug-logging)) are unchanged; the audit records are
additional.

## Volume

Each authenticated request through the middleware adds two `INFO` records, each context
block one more pair, and a Celery task or a per-tenant command run one pair. The test
helpers in `django_rls_tenants.tenants.testing` enter context managers too, so a test run
logs `context_manager` records. Refusals and
the other `WARNING` events are rare in a correctly configured application, so routing only
`WARNING` and above to an alerting channel gives a short list of what to look at:

```python title="settings.py"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "audit_trail": {"class": "logging.FileHandler", "filename": "/var/log/app/rls-audit.log"},
        "audit_alerts": {"class": "logging.StreamHandler", "level": "WARNING"},
    },
    "loggers": {
        "django_rls_tenants.audit": {
            "handlers": ["audit_trail", "audit_alerts"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
```

The logger checks its level before it builds a record, so with the audit logger at
`WARNING` or above the `INFO` events cost one level check each.

## Keeping the trail trustworthy

The records describe what the library did; they are not tamper-proof by themselves.
Send them to an append-only store (a log service, a write-once bucket) that the
application's database role and its operators cannot edit, and keep its retention in line
with your compliance requirements.
