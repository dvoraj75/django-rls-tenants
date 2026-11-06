"""A small REST API for notes, built on ``django_rls_tenants.contrib.ninja``.

Django Ninja authenticates inside the operation, after ``RLSTenantMiddleware``
has run, so a client that sends a token reaches the middleware as an anonymous
user. ``RLSTenantNinjaAPI`` opens the RLS context once Ninja knows the user
(``request.auth`` is the tenant user the token belongs to); ``RLSTenantGuard``
refuses a request served in any other context; the schemas hide the tenant FK
and ``rls_save()`` stamps it from the context and checks the ``category``
against the tenant. There is no ``.filter()`` and no ``tenant=`` anywhere below.

Try it (the token is the DRF one, see ``notes/api.py``):

    docker compose exec web python manage.py drf_create_token alice
    curl -H "Authorization: Bearer <key>" http://localhost:8000/ninja/notes/
"""

from ninja import Router, Status
from ninja.security import HttpBearer, django_auth
from rest_framework.authtoken.models import Token

from django_rls_tenants.contrib.ninja import (
    RLSTenantGuard,
    RLSTenantModelSchema,
    RLSTenantNinjaAPI,
    arls_save,
    rls_save,
)

from .models import Note


class TokenAuth(HttpBearer):
    """Authenticate with a DRF token; ``request.auth`` is the token's user."""

    def authenticate(self, request, token):
        found = Token.objects.select_related("user").filter(key=token).first()
        return found.user if found else None


class NoteOut(RLSTenantModelSchema):
    class Meta:
        model = Note
        fields = ["id", "title", "content", "category", "created_at"]


class NoteIn(RLSTenantModelSchema):
    class Meta:
        model = Note
        fields = ["title", "content", "category"]


router = Router(tags=["notes"])


@router.get("/notes/", response=list[NoteOut])
def list_notes(request):
    # A lazy queryset: Ninja serializes it inside the context the API entered,
    # and the ORM adds the tenant filter.
    return Note.objects.all()


@router.post("/notes/", response={201: NoteOut})
def create_note(request, payload: NoteIn):
    # rls_save() takes the tenant from the context (a payload cannot set it)
    # and answers 422 for a category that is not the tenant's.
    # exclude_unset: a field the client left out keeps the model default.
    return Status(201, rls_save(request, Note(), payload, exclude_unset=True))


@router.post("/notes/async/", response={201: NoteOut})
async def create_note_async(request, payload: NoteIn):
    return Status(201, await arls_save(request, Note(), payload, exclude_unset=True))


api = RLSTenantNinjaAPI(
    auth=[TokenAuth(), django_auth],
    rls_guard=RLSTenantGuard(),
    urls_namespace="ninja-notes",
)
api.add_router("", router)
