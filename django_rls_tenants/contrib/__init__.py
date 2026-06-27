"""Optional integrations with third-party libraries.

Each module in this subpackage glues django-rls-tenants to a framework that the
core library deliberately does **not** depend on. Importing this package is
always safe; importing a specific integration module (for example
:mod:`django_rls_tenants.contrib.celery`) requires the corresponding extra to be
installed and raises a helpful :class:`ImportError` otherwise.

The core ``tenants/`` and ``rls/`` layers never import anything from here, so a
project that does not use Celery never pays for it.
"""

from __future__ import annotations
