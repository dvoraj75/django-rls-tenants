"""Celery application for the demo project.

The demo runs Celery in **eager mode** (see the ``CELERY_*`` settings in
``demo/settings.py``): ``task.delay()`` executes the task inline in the calling
process, so no broker and no worker are needed. The RLS context still travels
through the task *headers* exactly as it would to a real worker -- see
``notes/tasks.py`` for the ``@rls_task`` functions that rely on it.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

app = Celery("demo")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
