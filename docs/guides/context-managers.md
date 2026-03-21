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

!!! note
    Nesting only saves/restores when `USE_LOCAL_SET` is `False` (the default).
    With `USE_LOCAL_SET=True`, GUC values are transaction-scoped and PostgreSQL
    handles cleanup at transaction boundaries.

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

1. At **decoration time**: caches `inspect.signature()` of the wrapped function.
2. At **call time**: extracts the user argument by name from `*args`/`**kwargs`.
3. If the user is an **admin** (`is_tenant_admin=True`): wraps in `admin_context()`.
4. If the user is a **tenant user**: wraps in `tenant_context(user.rls_tenant_id)`.
5. If the user is **`None`**: logs a warning and proceeds without context (fail-closed).

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
user argument is provided, so decorated functions pass the strict mode check.

See [Configuration](../getting-started/configuration.md#strict_mode) for setup
instructions.

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
