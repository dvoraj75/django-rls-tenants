-- Create a non-superuser role for the Django application.
-- PostgreSQL superusers bypass RLS entirely, so the app must connect
-- with a regular role for tenant isolation to work.
CREATE ROLE app WITH LOGIN PASSWORD 'app';
GRANT ALL PRIVILEGES ON DATABASE demo TO app;

-- Allow the app role to create and manage objects in the public schema.
GRANT ALL ON SCHEMA public TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO app;
