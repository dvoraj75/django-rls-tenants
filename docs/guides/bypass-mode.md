# Bypass Mode

Bypass mode allows specific operations to read data across tenant boundaries. This
is necessary for authentication, admin dashboards, analytics, and other cross-tenant
operations.

## Admin Bypass

The primary bypass mechanism is the admin flag. When `rls.is_admin` is set to `'true'`,
the RLS policy allows access to all rows:

```python
from django_rls_tenants import admin_context

with admin_context():
    # All rows visible regardless of tenant
    all_users = User.objects.all()
```

This is used by:

- `admin_context()` context manager
- `RLSTenantMiddleware` when the user has `is_tenant_admin=True`
- `rls_bypass()` test helper (a convenience wrapper around `admin_context`)

## Extra Bypass Flags

For edge cases where you need read-only bypass without full admin access, use
`extra_bypass_flags` on the `RLSConstraint`:

```python
from django_rls_tenants.rls import RLSConstraint
from django_rls_tenants import RLSProtectedModel


class ProtectedUser(RLSProtectedModel):
    # ...

    class Meta(RLSProtectedModel.Meta):
        constraints = [
            RLSConstraint(
                field="tenant",
                name="%(app_label)s_%(class)s_rls_constraint",
                extra_bypass_flags=["rls.auth_bypass"],
            ),
        ]
```

### How Extra Bypass Flags Differ from Admin

| Behavior | Admin bypass | Extra bypass flags |
|----------|-------------|-------------------|
| Added to `USING` clause (SELECT/UPDATE/DELETE) | Yes | Yes |
| Added to `WITH CHECK` clause (INSERT/UPDATE) | Yes | **No** |
| Allows reading all rows | Yes | Yes |
| Allows inserting/updating without tenant | Yes | **No** |

Extra bypass flags are intentionally read-only. This prevents accidental writes
without proper tenant context.

### Setting Bypass Flags

Use the imperative helpers or context manager:

```python
from django_rls_tenants.tenants.bypass import set_bypass_flag, clear_bypass_flag, bypass_flag

# Context manager (recommended):
with bypass_flag("rls.auth_bypass"):
    user = User.objects.get(email="admin@example.com")

# Imperative (for middleware-style code):
set_bypass_flag("rls.auth_bypass")
try:
    user = User.objects.get(email="admin@example.com")
finally:
    clear_bypass_flag("rls.auth_bypass")
```

### Use Case: Authentication

During authentication, the user record must be read before the tenant context is known.
A bypass flag allows the auth backend to read user records without setting a full admin
context:

```python title="myapp/auth.py"
from django_rls_tenants.tenants.bypass import bypass_flag


class RLSAuthBackend:
    def authenticate(self, request, username=None, password=None):
        from myapp.models import User

        with bypass_flag("rls.auth_bypass"):
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                return None

        if user.check_password(password):
            return user
        return None
```

## Security Implications

!!! warning
    Bypass modes weaken tenant isolation. Use them only when necessary and for the
    narrowest scope possible.

Guidelines:

1. **Prefer `tenant_context`** over `admin_context` when you know the tenant.
2. **Use `extra_bypass_flags`** instead of `admin_context` when you only need read access.
3. **Keep bypass blocks small** -- enter and exit as quickly as possible.
4. **Never expose bypass to end users** -- bypass should only be used in server-side code
   that you control.
5. **Audit bypass usage** -- search your codebase for `admin_context`, `rls_bypass`, and
   `bypass_flag` to understand your bypass surface area.

## When Bypass Is Needed

| Scenario | Recommended approach |
|----------|---------------------|
| Authentication backends | `extra_bypass_flags` with `bypass_flag()` context manager |
| Admin dashboards | `admin_context()` or `is_tenant_admin=True` on the user |
| Cross-tenant analytics | `admin_context()` |
| Data migrations | `admin_context()` |
| Management commands | `admin_context()` or `tenant_context()` depending on scope |
| Background tasks | `tenant_context(tenant_id)` when tenant is known |
