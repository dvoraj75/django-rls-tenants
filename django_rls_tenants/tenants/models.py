"""RLSProtectedModel -- abstract base for tenant-scoped models.

Uses ``__init_subclass__`` to dynamically create a ``ForeignKey`` to the
configured tenant model, and attaches ``RLSConstraint`` + ``RLSManager``.
"""

from __future__ import annotations
