# Configuration

## Settings

All configuration is provided through the `RLS_TENANTS` dictionary in your Django
settings module.

```python
RLS_TENANTS = {
    # Required: dotted path to your tenant model
    "TENANT_MODEL": "myapp.Tenant",

    # Name of the ForeignKey field on RLSProtectedModel subclasses
    # Default: "tenant"
    "TENANT_FK_NAME": "tenant",

    # PostgreSQL GUC variable name used to pass tenant ID to RLS policies
    # Default: "rls.tenant_id"
    "GUC_NAME": "rls.tenant_id",

    # Use SET LOCAL (transaction-scoped) instead of set_config (session-scoped)
    # Recommended for connection pooling (PgBouncer, pgpool)
    # Default: False
    "USE_LOCAL_SET": False,
}
```

## Environment Variables

The test suite reads database configuration from environment variables:

| Variable          | Default                       |
|-------------------|-------------------------------|
| `POSTGRES_DB`     | `django_rls_tenants_test`     |
| `POSTGRES_USER`   | `postgres`                    |
| `POSTGRES_PASSWORD` | `postgres`                  |
| `POSTGRES_HOST`   | `localhost`                   |
| `POSTGRES_PORT`   | `5432`                        |
