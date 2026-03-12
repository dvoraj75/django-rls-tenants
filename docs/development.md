# Development

## Prerequisites

- Python 3.11+
- PostgreSQL 15+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
git clone https://github.com/dvoraj75/django-rls-tenants.git
cd django-rls-tenants
uv sync --group dev --group test
uv run pre-commit install
```

## Running Tests

```bash
# All tests
uv run pytest

# With coverage
uv run pytest --cov

# Only RLS layer tests
uv run pytest tests/test_rls/

# Only integration tests
uv run pytest -m integration
```

## Linting and Type Checking

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy django_rls_tenants
```

## Project Structure

```
django-rls-tenants/
├── django_rls_tenants/         # Package source (flat layout)
│   ├── rls/                    # Generic RLS primitives
│   ├── tenants/                # Django multitenancy layer
│   └── management/commands/    # Management commands
├── tests/                      # Test suite
│   ├── test_rls/               # rls/ layer unit tests
│   ├── test_tenants/           # tenants/ layer unit tests
│   ├── test_integration/       # End-to-end tests
│   └── test_layering.py        # Layer boundary enforcement
├── docs/                       # Documentation
├── .github/                    # CI/CD and GitHub config
└── plan/                       # RFC and implementation plans
```

## Conventions

- All modules use `from __future__ import annotations`.
- Internal (private) modules use `_underscore` prefix.
- The `rls/` layer must not import from `tenants/`.
- Tests mirror the source structure (`test_rls/`, `test_tenants/`).
