FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Install the library from local source
COPY pyproject.toml README.md /lib/
COPY django_rls_tenants/ /lib/django_rls_tenants/
RUN pip install --no-cache-dir /lib/ psycopg2-binary "django>=5.1,<5.2"

# Copy the example project
COPY example/ /app/

EXPOSE 8000
