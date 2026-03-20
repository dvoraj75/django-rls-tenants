# django-rls-tenants Demo

A multi-tenant notes application demonstrating
**[django-rls-tenants](https://github.com/dvoraj75/django-rls-tenants)** --
database-enforced tenant isolation using PostgreSQL Row-Level Security (RLS).

> The view code uses `Note.objects.select_related("category").all()` with
> **zero** `.filter()` calls -- PostgreSQL RLS policies and automatic query
> scoping handle tenant isolation at both the ORM and database level.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) & Docker Compose (recommended)
- **Or** Python 3.12+ and PostgreSQL 16+ for local development

## Quick Start (Docker)

```bash
cd example/
docker compose up
```

Open **<http://localhost:8000>** and log in with any demo account:

| Username | Password    | Tenant     | Sees              |
| -------- | ----------- | ---------- | ----------------- |
| `alice`  | `demo1234`  | Acme Corp  | Only Acme notes   |
| `bob`    | `demo1234`  | Globex Inc | Only Globex notes |
| `carol`  | `demo1234`  | Initech    | Only Initech notes|
| `admin`  | `admin1234` | _(admin)_  | **All notes**     |

## What to Try

1. **Tenant isolation** -- Log in as Alice, see only Acme notes and categories.
   Switch to Bob and see only Globex data. No `.filter()` in the view -- RLS
   does it all.

2. **Admin bypass** -- Log in as `admin@example.com` and see every note across
   all tenants.

3. **Categories with `select_related()`** -- Notes display their category inline.
   The view uses `Note.objects.select_related("category")`, and the tenant filter
   automatically propagates to the joined `Category` table.

4. **Statistics page** -- Click "Stats" to see per-category note counts. This
   page is powered by a service function decorated with `@with_rls_context`,
   demonstrating automatic RLS context from function arguments.

5. **Database-level proof** -- Open a raw database shell and verify RLS is
   active:
   ```bash
   docker compose exec web python manage.py dbshell
   ```
   ```sql
   -- Without tenant context, RLS returns zero rows:
   SELECT count(*) FROM notes_note;
   -- -> 0
   ```

6. **RLS policy verification** -- The `check_rls` management command verifies
   all RLS policies are correctly applied:
   ```bash
   docker compose exec web python manage.py check_rls
   ```

7. **Strict mode** -- The demo has `STRICT_MODE=True` in `RLS_TENANTS`. Try
   querying an RLS-protected model in `manage.py shell` without establishing
   a tenant context:
   ```python
   from notes.models import Note
   Note.objects.count()  # -> raises NoTenantContextError
   ```
   This catches accidental unscoped queries during development. The
   `note_delete` view in `notes/views.py` shows how to handle the error
   gracefully.

8. **Django admin** -- Visit <http://localhost:8000/admin/>
   (`admin@example.com` / `admin1234`).

## Library Features Demonstrated

| Feature | Where |
| ------- | ----- |
| `RLSProtectedModel` | `notes/models.py` -- `Note` and `Category` |
| `RLSTenantMiddleware` | `demo/settings.py` -- automatic per-request RLS context |
| `TenantUser` protocol | `accounts/models.py` -- `rls_tenant_id` + `is_tenant_admin` |
| Auto-scoping (zero `.filter()`) | `notes/views.py` -- `Note.objects.all()` returns only tenant data |
| `select_related()` propagation | `notes/views.py` -- tenant filter auto-propagates to joined `Category` |
| `@with_rls_context` decorator | `notes/services.py` -- `get_note_stats(as_user=...)` |
| `admin_context()` | `seed_demo.py` -- bulk data creation bypassing RLS |
| `tenant_context()` | `seed_demo.py` -- programmatic tenant scoping for verification |
| `check_rls` command | `docker-compose.yml` -- runs after migrations |
| `STRICT_MODE` | `demo/settings.py` -- raises `NoTenantContextError` on unscoped queries |
| `NoTenantContextError` handling | `notes/views.py` -- graceful handling in view code |
| Testing utilities | `tests/test_rls.py` -- `rls_bypass`, `rls_as_tenant`, assert helpers |
| Strict mode tests | `tests/test_rls.py` -- `TestStrictMode` class |

## How It Works

```
Request
  -> Django AuthenticationMiddleware (sets request.user)
  -> RLSTenantMiddleware:
       1. Sets PostgreSQL GUC variables (rls.current_tenant, rls.is_admin)
       2. Sets ContextVar for automatic ORM query scoping
  -> View calls Note.objects.select_related("category").all()
       1. RLSManager auto-adds WHERE tenant_id = X (from ContextVar)
       2. select_related() propagates tenant filter to Category join
  -> PostgreSQL RLS policy provides defense-in-depth at database level
```

## Project Structure

```
example/
├── docker-compose.yml   Docker Compose for db + web
├── Dockerfile           Builds from library source + example app
├── manage.py            Django management script
├── demo/                Django project: settings, urls, wsgi/asgi
├── tenants/             Tenant model (shared, not RLS-protected)
├── accounts/            Custom User model with tenant FK + TenantUser protocol
├── notes/               RLS-protected Note + Category models, views, services,
│                        templates, admin, seed command
└── tests/               Example tests using django-rls-tenants testing utilities
    ├── conftest.py      Fixtures using rls_bypass for test data setup
    └── test_rls.py      RLS policy verification and tenant isolation tests
```

## Running Tests

The example includes tests that demonstrate the library's testing utilities.
These require a running PostgreSQL instance:

```bash
# With Docker (exec into the running container):
docker compose exec web python -m pytest tests/ -v

# Or locally (with PostgreSQL running):
cd example/
pip install pytest pytest-django
python -m pytest tests/ -v
```

## Local Development (without Docker)

```bash
cd example/

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install the library from the parent directory
pip install -e ..
pip install psycopg2-binary

# Configure database (PostgreSQL required -- SQLite will not work)
# IMPORTANT: Use a non-superuser role -- superusers bypass RLS entirely.
# See docker/init-db.sql for the recommended role setup.
export POSTGRES_DB=demo POSTGRES_USER=app POSTGRES_PASSWORD=app
export POSTGRES_HOST=localhost POSTGRES_PORT=5432

# Run migrations, verify policies, seed data, and start the server
python manage.py makemigrations
python manage.py migrate
python manage.py check_rls
python manage.py seed_demo --no-input
python manage.py runserver
```

> **Important:** The PostgreSQL user must **not** be a superuser -- superusers
> bypass RLS entirely. If running PostgreSQL locally (without Docker), create a
> non-superuser role as shown in `docker/init-db.sql`.

## Learn More

- **[django-rls-tenants documentation](https://dvoraj75.github.io/django-rls-tenants/)** --
  full guide covering installation, configuration, models, middleware, testing,
  and more.
- **[django-rls-tenants on PyPI](https://pypi.org/project/django-rls-tenants/)** --
  `pip install django-rls-tenants`
