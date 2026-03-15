# django-rls-tenants Demo

A multi-tenant notes application demonstrating
**[django-rls-tenants](https://github.com/dvoraj75/django-rls-tenants)** --
database-enforced tenant isolation using PostgreSQL Row-Level Security (RLS).

> The view code uses `Note.objects.all()` with **zero** `.filter()` calls --
> PostgreSQL RLS policies handle tenant isolation automatically at the database
> level.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) & Docker Compose (recommended)
- **Or** Python 3.12+ and PostgreSQL 16+ for local development

## Quick Start (Docker)

```bash
cd example/
docker compose up
```

Open **<http://localhost:8000>** and log in with any demo account:

| User                | Password    | Tenant     | Sees              |
| ------------------- | ----------- | ---------- | ----------------- |
| `alice@acme.com`    | `demo1234`  | Acme Corp  | Only Acme notes   |
| `bob@globex.com`    | `demo1234`  | Globex Inc | Only Globex notes |
| `carol@initech.com` | `demo1234`  | Initech    | Only Initech notes|
| `admin@example.com` | `admin1234` | _(admin)_  | **All notes**     |

## What to Try

1. **Tenant isolation** -- Log in as Alice, see only Acme notes. Switch to Bob
   and see only Globex notes. No `.filter()` in the view -- RLS does it all.

2. **Admin bypass** -- Log in as `admin@example.com` and see every note across
   all tenants.

3. **Database-level proof** -- Open a raw database shell and verify RLS is
   active:
   ```bash
   docker compose exec web python manage.py dbshell
   ```
   ```sql
   -- Without tenant context, RLS returns zero rows:
   SELECT count(*) FROM notes_note;
   -- -> 0
   ```

4. **Django admin** -- Visit <http://localhost:8000/admin/>
   (`admin@example.com` / `admin1234`).

## How It Works

```
Request
  -> Django AuthenticationMiddleware (sets request.user)
  -> RLSTenantMiddleware (SETs PostgreSQL session variable from user.rls_tenant_id)
  -> View calls Note.objects.all()
  -> PostgreSQL RLS policy filters rows by tenant_id automatically
```

| File                  | Role                                                        |
| --------------------- | ----------------------------------------------------------- |
| `tenants/models.py`   | Plain Django model for tenants (shared, not RLS-protected)  |
| `accounts/models.py`  | Custom `User` implementing the `TenantUser` protocol        |
| `notes/models.py`     | `Note` inherits `RLSProtectedModel` -- that's it            |
| `demo/settings.py`    | `RLS_TENANTS` config and `RLSTenantMiddleware` registration |

## Project Structure

```
example/
├── docker-compose.yml   Docker Compose for db + web
├── Dockerfile           Builds from library source + example app
├── manage.py            Django management script
├── demo/                Django project: settings, urls, wsgi/asgi
├── tenants/             Tenant model (shared, not RLS-protected)
├── accounts/            Custom User model with tenant FK + TenantUser protocol
├── notes/               RLS-protected Note model, views, templates, seed command
└── docker/              PostgreSQL init script (creates non-superuser role)
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
export POSTGRES_DB=demo POSTGRES_USER=demo POSTGRES_PASSWORD=demo
export POSTGRES_HOST=localhost POSTGRES_PORT=5432

# Run migrations, seed data, and start the server
python manage.py makemigrations
python manage.py migrate
python manage.py seed_demo --no-input
python manage.py runserver
```

> **Important:** The PostgreSQL user must **not** be a superuser -- superusers
> bypass RLS entirely. See `docker/init-db.sql` for the recommended role setup.

## Learn More

- **[django-rls-tenants documentation](https://dvoraj75.github.io/django-rls-tenants/)** --
  full guide covering installation, configuration, models, middleware, testing,
  and more.
- **[django-rls-tenants on PyPI](https://pypi.org/project/django-rls-tenants/)** --
  `pip install django-rls-tenants`
