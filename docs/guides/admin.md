# Tenant-Aware Admin

The Django admin is where staff spend their day, but out of the box it has no
idea your data is tenant-scoped: a changelist shows every tenant's rows, the
add form asks which tenant a record belongs to, and related dropdowns leak
names across tenants. `RLSTenantModelAdmin` fixes that by running **every** admin
database operation inside the RLS context implied by the logged-in user.

```python
from django.contrib import admin
from django_rls_tenants import RLSTenantModelAdmin
from myapp.models import Order


@admin.register(Order)
class OrderAdmin(RLSTenantModelAdmin):
    list_display = ("product", "amount", "created_at")
    search_fields = ("product",)
```

That's the whole integration. Subclass `RLSTenantModelAdmin` instead of
`admin.ModelAdmin` and the admin becomes tenant-aware:

- **Changelists** show only the current tenant's rows.
- **The tenant FK** is hidden on the add/change form and filled in on save.
- **Related dropdowns** (and inline forms) list only the current tenant's rows.
- **Cross-tenant admins** get a tenant switcher in the changelist filters.
- **A user with no tenant** is denied (`PermissionDenied`) instead of seeing
  everything.

## Requirements

`RLSTenantModelAdmin` reads the logged-in user's RLS role from `request.user`,
which therefore must satisfy the
[`TenantUser`](../guides/user-integration.md) protocol — the same
`is_tenant_admin` / `rls_tenant_id` contract the middleware and `for_user()`
already use. If your `AUTH_USER_MODEL` implements that protocol (most projects
add the two properties to their `User`), you're done.

!!! tip "Pair it with the middleware"
    Add [`RLSTenantMiddleware`](middleware.md) to your `MIDDLEWARE`. The mixin
    wraps the per-model admin views it controls, but the admin also serves some
    **site-level** views — most importantly the autocomplete endpoint, which in
    Django 6.0 is no longer a `ModelAdmin` method. The middleware sets the RLS
    context for *every* request, so those views are scoped too. The mixin's
    per-view context simply nests inside the middleware's (the tenant switcher
    still wins for the views it wraps).

## How it works

Admin querysets are lazy — they are evaluated long after `get_queryset()`
returns, while the template renders. So instead of scoping the queryset,
`RLSTenantModelAdmin` wraps each database-touching view
(`changelist_view`, `add_view`, `change_view`, `delete_view`, `history_view`) in
a `tenant_context()` / `admin_context()` block that spans the **whole** view:

```python
def changelist_view(self, request, extra_context=None):
    self._sync_tenant_selection(request)
    with self._rls_context(request):
        return super().changelist_view(request, extra_context)
```

With the context active, [`RLSManager.get_queryset()`](../reference/api.md)
already adds `WHERE tenant_id = X` (or the admin bypass), so the changelist,
the related-field dropdowns, and the saves are all scoped automatically. The
mixin deliberately **does not** override `get_queryset()` — that would just
duplicate the filter the manager already applies.

### Resolving the effective tenant

| User                                    | Effective tenant            | Context           |
|-----------------------------------------|-----------------------------|-------------------|
| Scoped user (`is_tenant_admin=False`)   | their `rls_tenant_id`       | `tenant_context`  |
| Cross-tenant admin, a tenant selected   | the switcher selection      | `tenant_context`  |
| Cross-tenant admin, no selection        | — (all tenants)             | `admin_context`   |
| Scoped user with `rls_tenant_id=None`   | — (denied)                  | `PermissionDenied`|

## Hiding the tenant FK

When the effective tenant is **implicit** — a scoped user, or an admin who has
picked a tenant — the tenant foreign key is excluded from the form and set on
save, so staff never pick the tenant by hand (and can't pick the wrong one). The
same exclusion applies to any explicit `fieldsets` layout you define: the mixin
removes the FK from the layout as well, so you can safely include the field in
`fieldsets` without risking a `KeyError` at render time for scoped users. A
cross-tenant admin with **no** selection keeps the field visible in both the
form and the layout, because in that mode they're explicitly choosing which
tenant a new record belongs to.

`save_model()` stamps the effective tenant onto new objects, so an auto-hidden
FK is always populated correctly.

## The tenant switcher

Cross-tenant admins (`is_tenant_admin=True`) get a **tenant switcher** rendered
as a changelist filter listing every tenant from
`RLS_TENANTS["TENANT_MODEL"]`, plus an "All" entry:

- Picking a tenant scopes the changelist to it **and** persists the choice in
  the session, so it carries into the add/change/delete views.
- "All" clears the selection and drops back to `admin_context()` (all tenants).

Because the selection is session-backed, an admin can pick *Acme*, then add a
handful of records that are all assigned to *Acme* without re-selecting it on
every page.

## Configuration

All behaviour is controlled with class attributes — there are **no** new
`RLS_TENANTS` settings keys:

| Attribute                  | Default             | Purpose |
|----------------------------|---------------------|---------|
| `rls_allow_tenant_switch`  | `True`              | Give cross-tenant admins the switcher. Set `False` to pin them to `admin_context()` (all tenants, no switcher). |
| `rls_session_key`          | `"rls_admin_tenant"`| Session key the switcher selection is stored under. |
| `rls_tenant_query_param`   | `"rls_tenant"`      | Query parameter the switcher uses (e.g. `?rls_tenant=42`). |
| `rls_deny_without_tenant`  | `True`              | Raise `PermissionDenied` for a non-admin user with no tenant. Set `False` to fall through with no context (fail-closed: RLS returns nothing). |

```python
@admin.register(Invoice)
class InvoiceAdmin(RLSTenantModelAdmin):
    rls_allow_tenant_switch = False   # support staff only ever see their own tenant
    list_display = ("number", "total")
```

## Fail-closed

A non-admin user whose `rls_tenant_id` is `None` has no tenant to scope to.
Rather than silently granting `admin_context()` (which would expose every
tenant), the mixin raises `PermissionDenied` — carrying the same actionable hint
the rest of the library uses — so the misconfiguration surfaces as a clean 403:

```text
AdminUser is not a tenant admin and has no tenant assigned, so this admin cannot
determine which tenant's data to show. Hint: Assign the user to a tenant (set
rls_tenant_id) or mark them as a cross-tenant admin (is_tenant_admin=True). ...
```

Set `rls_deny_without_tenant = False` to fall through instead; with RLS enforced
the queries simply return nothing.

## Notes

- **Middleware interaction.** If `RLSTenantMiddleware` already established a
  context for the request, the mixin's context nests cleanly inside it
  (save/restore). A switcher selection overrides for the wrapped views; the
  predicate is idempotent, so there is no double-filtering.
- **Autocomplete.** Related-field autocomplete is a site-level admin view, so
  its scoping comes from `RLSTenantMiddleware` (see the tip above), not from the
  mixin. For a scoped user it is limited to their tenant; for a cross-tenant
  admin it lists all tenants.

!!! note "Sync only"
    Like the rest of v1.3.0, `RLSTenantModelAdmin` is synchronous. The standard
    Django admin is a sync WSGI application, so this is the expected mode.
