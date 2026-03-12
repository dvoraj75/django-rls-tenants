# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial project scaffolding and package structure.
- `rls/` layer: generic RLS primitives (GUC helpers, constraints, context managers).
- `tenants/` layer: Django multitenancy built on `rls/`.
- `RLSProtectedModel` abstract base with dynamic tenant FK.
- `TenantQuerySet` and `RLSManager` with lazy evaluation fix.
- `RLSTenantMiddleware` for request-scoped tenant context.
- `check_rls` management command.
- Testing utilities (`rls_bypass`, `rls_as_tenant`, assertion helpers).
