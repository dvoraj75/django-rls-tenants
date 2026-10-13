FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Install the library from local source
COPY pyproject.toml README.md /lib/
COPY django_rls_tenants/ /lib/django_rls_tenants/
# "[celery]" pulls in Celery for the @rls_task demo (notes/tasks.py).
# The Django series matches the locked Django that generated the committed migrations.
RUN pip install --no-cache-dir "/lib/[celery]" psycopg2-binary "django>=6.1,<6.2" \
    pytest pytest-django

# Copy the example project
COPY example/ /app/

EXPOSE 8000
