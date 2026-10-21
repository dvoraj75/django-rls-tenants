"""Test models exercising all django-rls-tenants features."""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models

from django_rls_tenants.rls.constraints import RLSConstraint
from django_rls_tenants.rls.predicates import SharedWith, TenantField
from django_rls_tenants.tenants.models import RLSProtectedModel


class Tenant(models.Model):
    """Not RLS-protected -- the tenant table itself is global."""

    name = models.CharField(max_length=100)

    class Meta:
        db_table = "test_tenant"

    def __str__(self) -> str:
        return self.name


class AdminUser(AbstractUser):
    """``AUTH_USER_MODEL`` for the admin tests; satisfies the ``TenantUser`` protocol.

    Django's admin needs a real authenticatable user (``is_staff`` /
    ``is_superuser`` / permissions), while ``RLSTenantModelAdmin`` reads the RLS
    role off the same ``request.user`` via the ``TenantUser`` protocol. A
    swapped-in auth user is the realistic way to have both on one object, exactly
    as a downstream project would wire its own ``User`` model.

    ``rls_admin`` is kept independent of ``is_superuser`` on purpose: a test user
    can be a Django superuser (so it reaches every admin view without per-model
    permission plumbing) while still being an RLS-scoped, non-admin tenant user,
    which is what the scoping tests need to exercise.
    """

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="admin_users",
    )
    rls_admin = models.BooleanField(default=False)

    class Meta:
        db_table = "test_admin_user"

    @property
    def is_tenant_admin(self) -> bool:
        """Cross-tenant RLS admin flag (decoupled from Django's ``is_superuser``)."""
        return self.rls_admin

    @property
    def rls_tenant_id(self) -> int | None:
        """Tenant PK for RLS filtering, or ``None`` for admins / unassigned users."""
        return self.tenant_id  # type: ignore[return-value]

    @property
    def rls_tenant_ids(self) -> frozenset[int]:
        """The tenants this user may select (``MultiTenantUser``, #47): their memberships.

        Not cached: a test adds memberships to a user object it already holds.
        """
        return frozenset(self.memberships.values_list("tenant_id", flat=True))


class Membership(models.Model):
    """A user's membership of a tenant (#47); a plain table, readable without a context."""

    user = models.ForeignKey(AdminUser, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")

    class Meta:
        db_table = "test_membership"
        constraints = [  # noqa: RUF012  -- Django Meta convention
            models.UniqueConstraint(fields=["user", "tenant"], name="test_membership_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.tenant_id}"


class TenantUser(models.Model):
    """Test user implementing the ``TenantUser`` protocol."""

    username = models.CharField(max_length=100)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    is_admin = models.BooleanField(default=False)

    class Meta:
        db_table = "test_user"

    def __str__(self) -> str:
        return self.username

    @property
    def is_tenant_admin(self) -> bool:
        """Return ``True`` if this user should bypass RLS."""
        return self.is_admin

    @property
    def rls_tenant_id(self) -> int | None:
        """Return the tenant ID for RLS filtering."""
        return self.tenant_id  # type: ignore[return-value]

    @property
    def is_authenticated(self) -> bool:
        """Always authenticated (test user)."""
        return True


class Order(RLSProtectedModel):
    """Standard RLS-protected model for testing."""

    product = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_order"


class Document(RLSProtectedModel):
    """Second RLS-protected model for cross-model isolation tests."""

    title = models.CharField(max_length=255)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_document"


class OrderItem(RLSProtectedModel):
    """RLS-protected model with FK to another RLS-protected model.

    Used to test ``select_related()`` tenant propagation across
    RLS-protected joins.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE)
    description = models.CharField(max_length=255)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_order_item"


class OrderNote(RLSProtectedModel):
    """RLS-protected model with a *nullable* FK to another RLS-protected model.

    Used to test ``select_related()`` tenant propagation preserves
    rows where the nullable FK is ``NULL`` (LEFT OUTER JOIN semantics).
    """

    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)
    body = models.CharField(max_length=255)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_order_note"


class OrderNoteAttachment(RLSProtectedModel):
    """RLS-protected model two hops away from ``Order`` via *nullable* FKs.

    Used to test ``select_related()`` on nested paths (#79):

    - ``note__order``: nullable intermediate (``note``) and nullable leaf
      (``OrderNote.order``).
    - ``item__order``: nullable intermediate (``item``) and non-null leaf
      (``OrderItem.order``).

    Both paths must keep LEFT OUTER JOIN semantics so rows whose chain is
    broken by a ``NULL`` at any hop are not dropped.
    """

    note = models.ForeignKey(OrderNote, on_delete=models.SET_NULL, null=True, blank=True)
    item = models.ForeignKey(OrderItem, on_delete=models.SET_NULL, null=True, blank=True)
    filename = models.CharField(max_length=255)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_order_note_attachment"


class NoteRevision(RLSProtectedModel):
    """RLS-protected model two hops away from ``Order`` via *non-null* first hops.

    Used to test ``select_related()`` on nested paths (#79):

    - ``note__order``: non-null intermediate (``note``) and nullable leaf
      (``OrderNote.order``) -- must stay a LEFT OUTER JOIN on the leaf.
    - ``item__order``: fully non-null chain -- an INNER JOIN is correct.
    """

    note = models.ForeignKey(OrderNote, on_delete=models.CASCADE)
    item = models.ForeignKey(OrderItem, on_delete=models.CASCADE)
    summary = models.CharField(max_length=255)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_note_revision"


class ProtectedUser(RLSProtectedModel):
    """RLS-protected user model with extra bypass flags."""

    email = models.CharField(max_length=255)
    tenant = models.ForeignKey(
        "test_app.Tenant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_protected_user"
        constraints = [  # noqa: RUF012  -- Django Meta convention
            RLSConstraint(
                field="tenant",
                name="test_app_protecteduser_rls_constraint",
                extra_bypass_flags=[
                    "rls.is_login_request",
                    "rls.is_preauth_request",
                ],
            ),
        ]


# ---------------------------------------------------------------------------
# M2M test models (Phase 3 - Issue #11)
# ---------------------------------------------------------------------------


class Tag(models.Model):
    """Non-RLS model for mixed M2M testing (one side RLS-protected)."""

    name = models.CharField(max_length=100)

    class Meta:
        db_table = "test_tag"

    def __str__(self) -> str:
        return self.name


class Project(RLSProtectedModel):
    """RLS-protected model with M2M relationships.

    - ``members``: Both sides RLS-protected (Project <-> ProtectedUser)
    - ``tags``: Only one side RLS-protected (Project <-> Tag)
    """

    name = models.CharField(max_length=100)
    members = models.ManyToManyField(ProtectedUser)
    tags = models.ManyToManyField(Tag)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_project"


class SelfRefModel(RLSProtectedModel):
    """RLS-protected model with a self-referential M2M for testing."""

    name = models.CharField(max_length=100)
    friends = models.ManyToManyField("self", symmetrical=False)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_selfref"


class Label(models.Model):
    """RLS-protected by its ``RLSConstraint`` alone, not by ``RLSProtectedModel`` (#159).

    It keeps Django's default manager, so it has no auto-scope filter: the
    policy is its only isolation, and a test filters on ``tenant`` itself.
    ``tags``: only this side is protected (Label <-> Tag).
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    tags = models.ManyToManyField(Tag)

    class Meta:
        db_table = "test_label"
        constraints = [  # noqa: RUF012  -- Django Meta convention
            RLSConstraint(field="tenant", name="%(app_label)s_%(class)s_rls_constraint"),
        ]

    def __str__(self) -> str:
        return self.name


class Board(RLSProtectedModel):
    """``labels``: both sides protected, the to side by a constraint alone (#159)."""

    name = models.CharField(max_length=100)
    labels = models.ManyToManyField(Label)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_board"


class Invoice(RLSProtectedModel):
    """RLS-protected model with a per-tenant unique number.

    Used by the DRF tests: ``RLSTenantSerializerMixin`` hides the tenant FK, and
    the ``UniqueConstraint`` that includes it must still be validated.
    """

    number = models.CharField(max_length=50)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_invoice"
        constraints = [  # noqa: RUF012  -- Django Meta convention
            *RLSProtectedModel.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "number"],
                name="test_invoice_tenant_number_uniq",
            ),
        ]


class OrderShipment(RLSProtectedModel):
    """RLS-protected model with a one-to-one field to ``Order``.

    Used to test ``prefetch_related()`` tenant filters on the forward
    (``shipment.order``) and reverse (``order.shipment``) one-to-one hops,
    which Django fetches through the base manager (#44). A one-to-one
    field is unique across tenants by nature, so ``tests/settings.py``
    lists it in ``UNIQUE_ACROSS_TENANTS`` (``W012``).
    """

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="shipment")
    carrier = models.CharField(max_length=100)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_order_shipment"


class OrderProxy(Order):
    """Proxy of ``Order``: it has no constraints of its own."""

    class Meta:
        proxy = True


class Assignment(RLSProtectedModel):
    """RLS-protected model for the cross-tenant foreign key validation tests (#107).

    - ``assignee``: a model whose tenant is nullable. An assignee with no
      tenant (a cross-tenant admin row of ``ProtectedUser``) is invisible to
      every tenant, so it counts as another tenant's row.
    - ``order``: a proxy of an RLS-protected model, checked as its concrete
      model.
    """

    assignee = models.ForeignKey(ProtectedUser, on_delete=models.CASCADE, null=True, blank=True)
    order = models.ForeignKey(OrderProxy, on_delete=models.CASCADE, null=True, blank=True)
    title = models.CharField(max_length=100)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_assignment"


class Loan(RLSProtectedModel):
    """A protected model with a foreign key to the external ``test_external_app.Book`` (#112).

    Joins into ``Book`` get the tenant filter on its own field (``owner``).
    """

    borrower = models.CharField(max_length=100)
    book = models.ForeignKey("test_external_app.Book", on_delete=models.CASCADE)

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_loan"


class SharedNote(RLSProtectedModel):
    """A protected model whose rows other tenants may read (#59).

    Its ``RLSConstraint`` has two ``extra_predicates``: a ``NoteShare`` row
    shares a note with a tenant, and ``partner`` names a tenant that may read
    it. They make the ``test_shared_note_tenant_sharing_policy``.
    """

    title = models.CharField(max_length=100)
    partner = models.ForeignKey(
        Tenant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="partner_notes",
    )

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_shared_note"
        constraints = [  # noqa: RUF012  -- Django Meta convention
            RLSConstraint(
                field="tenant",
                name="%(app_label)s_%(class)s_rls_constraint",
                extra_predicates=[
                    SharedWith(through="test_app.NoteShare", fk="note", tenant_fk="shared_with"),
                    TenantField("partner"),
                ],
            ),
        ]


class NoteShare(RLSProtectedModel):
    """Shares a ``SharedNote`` with the tenant in ``shared_with`` (#59).

    ``tenant`` is the note's tenant, who owns the share. ``TenantField``
    lets the ``shared_with`` tenant read the row too, which the
    ``SharedWith`` predicate of ``SharedNote`` needs (system check ``W018``).
    """

    note = models.ForeignKey(SharedNote, on_delete=models.CASCADE, related_name="shares")
    shared_with = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="received_shares",
    )

    class Meta(RLSProtectedModel.Meta):
        db_table = "test_note_share"
        constraints = [  # noqa: RUF012  -- Django Meta convention
            RLSConstraint(
                field="tenant",
                name="%(app_label)s_%(class)s_rls_constraint",
                extra_predicates=[TenantField("shared_with")],
            ),
        ]
