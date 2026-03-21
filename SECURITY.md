# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 1.2.x   | :white_check_mark: |
| 1.1.x   | :white_check_mark: |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in django-rls-tenants, please report it
responsibly. **Do not open a public issue.**

1. **GitHub Private Advisory** (preferred): Open a
   [private security advisory](https://github.com/dvoraj75/django-rls-tenants/security/advisories/new)
   on this repository.

2. **Email**: Send a detailed report to the maintainer listed in `pyproject.toml`.

### What to include

- A description of the vulnerability and its potential impact.
- Steps to reproduce the issue or a proof-of-concept.
- The version(s) affected.
- Any suggested fix, if you have one.

### What to expect

- **Acknowledgement** within 48 hours.
- **Status update** within 7 days with an assessment and remediation timeline.
- **Fix release** as soon as practical, typically within 30 days for confirmed
  vulnerabilities. A CVE will be requested when appropriate.
- Credit in the release notes (unless you prefer to remain anonymous).

## Scope

This policy covers the `django-rls-tenants` Python package. Security concerns
related to PostgreSQL itself, Django, or other dependencies should be reported to
their respective maintainers.

## Security Model

For details on the library's security design, threat model, and guarantees, see
the [Security Model](https://dvoraj75.github.io/django-rls-tenants/advanced/security/)
documentation.
