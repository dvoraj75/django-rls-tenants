"""A small REST API for notes, built on ``django_rls_tenants.contrib.drf``.

DRF authenticates inside the view, after ``RLSTenantMiddleware`` has run, so a
client that sends a token reaches the middleware as an anonymous user. The
mixin opens the RLS context once DRF knows the user; the permission refuses a
request served in any other context; the serializer hides the tenant FK and
stamps it from the context. There is no ``.filter()`` and no ``tenant=``
anywhere below.

Try it (``drf_create_token`` ships with DRF):

    docker compose exec web python manage.py drf_create_token alice
    curl -H "Authorization: Token <key>" http://localhost:8000/api/notes/
"""

from rest_framework import routers, viewsets

from django_rls_tenants.contrib.drf import RLSTenantAPIViewMixin, RLSTenantModelSerializer

from .models import Note


class NoteSerializer(RLSTenantModelSerializer):
    class Meta:
        model = Note
        fields = ["id", "title", "content", "category", "created_at"]
        read_only_fields = ["created_at"]


class NoteViewSet(RLSTenantAPIViewMixin, viewsets.ModelViewSet):
    # Authentication and permissions come from REST_FRAMEWORK in demo/settings.py.
    serializer_class = NoteSerializer

    def get_queryset(self):
        # Built per request, inside the context: the ORM adds the tenant filter
        # (a class-level ``queryset`` is built at import, outside any context,
        # and would rely on the RLS policy alone).
        return Note.objects.select_related("category").all()


class APIRootView(RLSTenantAPIViewMixin, routers.APIRootView):
    # DefaultRouter's root view has no mixin, so RLSTenantPermission (a default
    # permission class) would refuse it to token clients.
    pass


class Router(routers.DefaultRouter):
    APIRootView = APIRootView
