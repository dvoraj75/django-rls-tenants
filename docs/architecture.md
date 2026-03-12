# Architecture

## Overview

django-rls-tenants is organized into two internal layers with a strict dependency
boundary:

```
┌─────────────────────────────────────────────┐
│              tenants/ layer                  │
│  (Django multitenancy: models, middleware,   │
│   context managers, managers, testing)       │
│                                             │
│  Imports from: rls/, django, stdlib         │
├─────────────────────────────────────────────┤
│                rls/ layer                   │
│  (Generic PostgreSQL RLS primitives: GUC    │
│   helpers, RLSConstraint, context managers) │
│                                             │
│  Imports from: django, stdlib ONLY          │
└─────────────────────────────────────────────┘
```

The `rls/` layer has **zero imports** from `tenants/`. This boundary is enforced
by `tests/test_layering.py`.

## How It Works

1. **GUC Variables** -- PostgreSQL session parameters (e.g., `rls.tenant_id`) are
   set via `SET LOCAL` within a transaction to communicate the current tenant to
   RLS policies.

2. **RLS Policies** -- `CREATE POLICY` statements (managed via Django migrations
   through `RLSConstraint`) filter rows based on the GUC variable value.

3. **Context Managers** -- `tenant_context()` and `admin_context()` wrap database
   operations, setting and clearing GUC variables with save/restore nesting.

4. **Middleware** -- `RLSTenantMiddleware` reads `request.user.rls_tenant_id` and
   activates the appropriate context for the entire request.

5. **Fail-Closed** -- If no GUC variable is set, RLS policies return zero rows.
   This prevents accidental data exposure.

## Design Decisions

See [plan/implementation-plan.md](../plan/implementation-plan.md) for the full
list of design decisions and their rationale.
