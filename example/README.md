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
| `staff`  | `staff1234` | Acme Corp  | Only Acme notes, also in `/admin/` |
| `admin`  | `admin1234` | _(admin)_  | **All notes**, with a tenant switcher in `/admin/` |

## What to Try

1. **Tenant isolation** -- Log in as Alice, see only Acme notes and categories.
   Switch to Bob and see only Globex data. No `.filter()` in the view -- RLS
   does it all.

2. **Admin bypass** -- Log in as `admin` and see every note across
   all tenants.

3. **Categories with `select_related()`** -- Notes display their category inline.
   The view uses `Note.objects.select_related("category")`, and the tenant filter
   automatically propagates to the joined `Category` table.

4. **Statistics page** -- Click "Stats" to see per-category note counts. This
   page is powered by a service function decorated with `@with_rls_context`,
   demonstrating automatic RLS context from function arguments.

5. **Database-level proof** -- Connect to PostgreSQL as `app`, the
   non-superuser role the demo runs as (see `docker/init-db.sql`), and query a
   protected table with no tenant context:
   ```bash
   docker compose exec db psql -U app -d demo -c 'SELECT count(*) FROM notes_note;'
   # -> 0 (no tenant context)
   ```
   Compare with the `demo` superuser, again from your host terminal:
   ```bash
   docker compose exec db psql -U demo -d demo -c 'SELECT count(*) FROM notes_note;'
   # -> 15 on a freshly seeded database
   ```
   Superusers (and roles with `BYPASSRLS`) bypass RLS and see every row, which is
   why the app must never connect as one. The total changes if you add or delete notes.

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

8. **Tenant-aware Django admin** -- `notes/admin.py` registers `Note` and
   `Category` with `RLSTenantModelAdmin` instead of `admin.ModelAdmin`. That is
   the whole integration; try it at <http://localhost:8000/admin/>:

   - Log in as `staff` / `staff1234` (Acme staff: `is_staff=True`,
     `is_tenant_admin=False`). The Notes and Categories changelists show only
     Acme rows with no `tenant` filter and no `.filter()` anywhere. Open
     "Add note": the `tenant` field is not on the form, and the Category
     dropdown lists only Acme categories. Save a note and it is stamped with
     Acme automatically.
   - Log in as `admin` / `admin1234` (`is_tenant_admin=True`). The changelists
     show every tenant, and a **"By tenant"** switcher appears in the filter
     sidebar. Pick *Acme Corp*: the list is scoped and, because the choice is
     kept in the session, "Add note" now hides and auto-assigns `tenant` too.
     Pick *All* to go back to every tenant (and get the `tenant` field back).

   > **Note:** an account made with `createsuperuser` has `tenant=None` and
   > `is_tenant_admin=False`, which is neither role. In this demo
   > `RLSTenantMiddleware` rejects such a user on every authenticated request
   > with `NoTenantContextError` (and `RLSTenantModelAdmin` alone would answer
   > `403`), so either assign it a tenant or set `is_tenant_admin=True`:
   >
   > ```bash
   > docker compose exec web python manage.py shell -c \
   >   "from accounts.models import User; User.objects.filter(username='me').update(is_tenant_admin=True)"
   > ```
   >
   > For an existing demo, rebuild with `docker compose up --build`. The seed
   > command adds the missing `staff` account while preserving existing data.

## Library Features Demonstrated

| Feature | Where |
| ------- | ----- |
| `RLSProtectedModel` | `notes/models.py` -- `Note` and `Category` |
| `RLSTenantMiddleware` | `demo/settings.py` -- automatic per-request RLS context |
| `TenantUser` protocol | `accounts/models.py` -- `rls_tenant_id` + `is_tenant_admin` |
| `RLSTenantModelAdmin` | `notes/admin.py` -- auto-scoped changelists, hidden + auto-assigned tenant FK, tenant switcher |
| Auto-scoping (zero `.filter()`) | `notes/views.py` -- `Note.objects.all()` returns only tenant data |
| `select_related()` propagation | `notes/views.py` -- tenant filter auto-propagates to joined `Category` |
| `@with_rls_context` decorator | `notes/services.py` -- `get_note_stats(as_user=...)` |
| `admin_context()` | `seed_demo.py` -- bulk data creation bypassing RLS |
| `tenant_context()` | `seed_demo.py` -- programmatic tenant scoping for verification |
| `check_rls` command | `docker-compose.yml` -- runs after migrations |
| `STRICT_MODE` | `demo/settings.py` -- raises `NoTenantContextError` on unscoped queries |
| `NoTenantContextError` handling | `notes/views.py` -- graceful handling in view code |
| Testing utilities | `tests/test_rls.py` -- `rls_bypass`, `rls_as_tenant`, assert helpers |
| Admin tests | `tests/test_admin.py` -- the tenant-aware admin driven through the test client |
| Seed command tests | `tests/test_seed_demo.py` -- `staff` account permissions and idempotent re-seeding |
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
    ├── test_admin.py    RLSTenantModelAdmin scoping, FK hiding and switcher tests
    ├── test_rls.py      RLS policy verification and tenant isolation tests
    └── test_seed_demo.py  Seeded staff account and idempotent re-seeding tests
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
python manage.py makemigrations
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
# IMPORTANT: Use a NOSUPERUSER NOBYPASSRLS role -- superusers and BYPASSRLS roles
# bypass RLS entirely.
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

> **Important:** The PostgreSQL user must **not** be a superuser or have
> `BYPASSRLS` -- both bypass RLS entirely. If running PostgreSQL locally
> (without Docker), create a plain role as shown in `docker/init-db.sql`.

## Learn More

- **[django-rls-tenants documentation](https://dvoraj75.github.io/django-rls-tenants/)** --
  full guide covering installation, configuration, models, middleware, testing,
  and more.
- **[django-rls-tenants on PyPI](https://pypi.org/project/django-rls-tenants/)** --
  `pip install django-rls-tenants`
