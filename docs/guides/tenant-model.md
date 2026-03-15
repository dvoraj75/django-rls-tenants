# Tenant Model

The tenant model is the central entity that represents a tenant in your application.
django-rls-tenants does not ship a built-in tenant model -- you define your own.

## Basic Tenant Model

A tenant model is any Django model with an integer or UUID primary key:

```python title="myapp/models.py"
from django.db import models


class Tenant(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name
```

Register the model in your settings:

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
}
```

## Primary Key Types

The RLS policy casts the GUC variable value to match the tenant PK type. Three SQL
cast types are supported:

### Integer (default)

```python
class Tenant(models.Model):
    # Django's default AutoField (integer PK)
    name = models.CharField(max_length=255)
```

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "TENANT_PK_TYPE": "int",  # default
}
```

### BigInteger

```python
class Tenant(models.Model):
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=255)
```

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "TENANT_PK_TYPE": "bigint",
}
```

### UUID

```python
import uuid

class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
```

```python title="settings.py"
RLS_TENANTS = {
    "TENANT_MODEL": "myapp.Tenant",
    "TENANT_PK_TYPE": "uuid",
}
```

!!! note
    The `TENANT_PK_TYPE` setting controls the SQL `::type` cast in the RLS policy.
    It must match your tenant model's actual primary key type or the policy will fail
    with a cast error at query time.

## The Tenant Model Is Not Protected

The tenant model itself should **not** inherit from `RLSProtectedModel`. It is the
root entity that other models reference -- it doesn't belong to a tenant; it *is*
the tenant.

```python
# Correct: Tenant is a plain model
class Tenant(models.Model):
    name = models.CharField(max_length=255)

# Correct: Order is protected by RLS
class Order(RLSProtectedModel):
    title = models.CharField(max_length=255)
    # tenant FK is added automatically
```

## Using an Existing Model

You can use any existing model as your tenant, including models from third-party apps,
as long as it has a supported PK type. The model does not need any special methods or
mixins.

```python title="settings.py"
# Using a model from a third-party app
RLS_TENANTS = {
    "TENANT_MODEL": "organizations.Organization",
    "TENANT_FK_FIELD": "organization",
}
```
