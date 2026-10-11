# Load the Celery app when Django starts so the @rls_task functions in
# notes/tasks.py register with it (the standard Django + Celery layout).
from .celery import app as celery_app

__all__ = ("celery_app",)
