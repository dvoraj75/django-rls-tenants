-- Create a non-superuser role for the Django application.
-- PostgreSQL superusers and roles with BYPASSRLS bypass RLS entirely, so the
-- app must connect with a regular (NOSUPERUSER NOBYPASSRLS) role for tenant
-- isolation to work. CREATE ROLE defaults to both, which is what this relies on.
CREATE ROLE app WITH LOGIN PASSWORD 'app' CREATEDB;
GRANT ALL PRIVILEGES ON DATABASE demo TO app;

-- Allow the app role to create and manage objects in the public schema.
GRANT ALL ON SCHEMA public TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO app;
