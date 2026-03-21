# Management Commands

## check_rls

The `check_rls` management command verifies that all `RLSProtectedModel` subclasses
have the expected RLS policies applied in the database.

### Usage

```bash
python manage.py check_rls
python manage.py check_rls --database replica  # check a specific database
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
  run: python manage.py check_rls
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--database` | `default` | Database alias to check RLS status on |

### Limitations

- Does not verify the **content** of the RLS policy (e.g., correct GUC variable names).
  It only checks that a policy exists.
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

### When to Use

- **After upgrading** to a version with M2M RLS support, if your existing M2M tables
  don't have policies yet.
- **After adding M2M fields** to existing `RLSProtectedModel` subclasses without
  creating a migration with `AddM2MRLSPolicy`.

!!! tip
    For new M2M fields, prefer using `AddM2MRLSPolicy` in a migration instead.
    `setup_m2m_rls` is a convenience for retroactive application.

## Using RLS in Custom Management Commands

Management commands run outside the request/response cycle, so the middleware does not
set any RLS context. Use context managers to set the context explicitly:

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
