"""Django management commands package for django-rls-tenants.

Provides CLI tools for verifying and applying Row-Level Security (RLS)
policies on protected tables, and for running other commands inside a tenant's
RLS context:

- ``check_rls``: Verify that RLS policies are enabled and correctly
  configured on all ``RLSProtectedModel`` subclasses and M2M through tables,
  and say how to fix each failing table. ``--generate`` writes the
  ``AddM2MRLSPolicy`` migrations the M2M through tables lack.
- ``setup_m2m_rls``: Apply RLS policies retroactively to M2M through
  tables on existing deployments without re-running migrations.
- ``run_as_tenant``: Run another management command inside one tenant's
  ``tenant_context()`` (or ``admin_context()`` with ``--admin``).
- ``run_for_all_tenants``: Run another management command once per tenant, each
  inside its own ``tenant_context()``, with filtering, ``--parallel`` workers,
  ``--json`` reports and a failure summary.
"""
