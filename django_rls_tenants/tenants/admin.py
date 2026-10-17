"""Tenant-aware Django admin: :class:`RLSTenantModelAdmin`.

``RLSTenantModelAdmin`` is a ``ModelAdmin`` mixin that runs every admin database
operation inside the RLS context implied by the logged-in user, so the admin
respects tenant isolation with no per-view boilerplate:

- **Auto-scoped context.** Each DB-touching admin view is wrapped in
  ``tenant_context()`` / ``admin_context()`` derived from ``request.user`` (which
  must satisfy the :class:`~django_rls_tenants.tenants.types.TenantUser`
  protocol, exactly like the middleware and ``for_user()`` expect). The wrapper
  spans the whole view, so lazy changelist querysets, related-field dropdowns,
  and saves all see the same context.
- **Reuses the manager's auto-scoping.** With the context active,
  :meth:`RLSManager.get_queryset` already adds ``WHERE tenant_id = X`` (or the
  admin bypass), so changelists and related dropdowns are scoped automatically --
  the mixin deliberately does **not** re-filter them.
- **Implicit tenant FK.** When the effective tenant is known (a scoped user, or a
  switching admin who has picked a tenant) the tenant foreign key is hidden from
  the form (and dropped from any explicit ``fieldsets``) and set on save. A global
  admin with no selection keeps the field visible so they can assign a tenant.
- **Tenant switcher.** Cross-tenant admins get a session-backed switcher rendered
  as a changelist filter; picking a tenant scopes every subsequent view, and the
  "All" entry clears it back to ``admin_context()``. A user who belongs to several
  tenants (``MultiTenantUser``) gets it too, listing only their tenants and without
  "All"; their choice is the site's tenant selection (``switch_tenant()``).
- **Fail-closed.** A non-admin user with no tenant raises ``PermissionDenied``
  (with the shared #34 hint) rather than silently falling back to admin access.

This module lives in ``tenants/`` (not a root ``admin.py``) on purpose:
``admin.autodiscover()`` force-imports ``<app>.admin`` for every installed app,
and importing ``django_rls_tenants.admin`` at that point would run before the app
registry settled. ``RLSTenantModelAdmin`` is re-exported lazily from the
top-level package instead.
"""

from __future__ import annotations

from contextlib import nullcontext
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from django.apps import apps
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms import ModelChoiceField

from django_rls_tenants.tenants import audit
from django_rls_tenants.tenants.conf import rls_tenants_config
from django_rls_tenants.tenants.context import admin_context, tenant_context
from django_rls_tenants.tenants.errors import HINT_USER_NO_TENANT
from django_rls_tenants.tenants.selection import (
    _clear,
    _other_tenants,
    _request_tenant_id,
    _selectable,
    _selects_tenants,
    _shown,
    _switch,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from django.contrib.admin.filters import _ListFilterChoices
    from django.contrib.admin.options import _FieldOpts, _FieldsetSpec
    from django.contrib.admin.views.main import ChangeList
    from django.db.models import Model
    from django.forms import ModelForm
    from django.http import HttpRequest, HttpResponse
    from django.utils.datastructures import _ListOrTuple

# Sentinel value emitted by the switcher's "All" entry. It clears any persisted
# selection so a cross-tenant admin drops back to admin_context() (all tenants).
# A real value rather than an absent parameter is required so the view can tell
# "show all" apart from ordinary paramless navigation (add/change/delete pages),
# which must keep the current selection.
_ALL_TENANTS = "__all__"

# The 403 of a member's switcher choice that a link on another site made.
_CROSS_SITE_MESSAGE = "The tenant switcher only accepts a choice made on this site."

# Default session key; mirrors RLSTenantModelAdmin.rls_session_key so the filter
# can read the persisted selection without a hard dependency on the admin class.
_DEFAULT_SESSION_KEY = "rls_admin_tenant"


def _fieldsets_without(
    fieldsets: _FieldsetSpec,
    field: str,
) -> list[tuple[Any, _FieldOpts]]:
    """Return *fieldsets* with *field* removed from every section's ``fields``.

    Handles both flat entries (``"tenant"``) and grouped entries that put several
    fields on one line (``("amount", "tenant")``); a group left empty is dropped.
    Used to keep an excluded tenant FK out of an explicit ``fieldsets`` layout --
    otherwise Django renders a layout that still names a field the (excluded) form
    no longer has, raising ``KeyError``. Builds fresh option dicts so the admin's
    own ``fieldsets`` attribute is never mutated.
    """
    cleaned: list[tuple[Any, _FieldOpts]] = []
    for name, opts in fieldsets:
        kept: list[Any] = []
        for entry in opts.get("fields", ()):
            if isinstance(entry, (list, tuple)):
                group = tuple(f for f in entry if f != field)
                if group:
                    kept.append(group)
            elif entry != field:
                kept.append(entry)
        cleaned.append((name, cast("_FieldOpts", {**opts, "fields": kept})))
    return cleaned


def _same_origin_navigation(request: HttpRequest) -> bool:
    """Return whether the browser reports that the request comes from this site.

    ``Sec-Fetch-Site`` is ``same-origin`` for a link on this site and ``none``
    for an address the user typed or bookmarked; a browser that does not send
    it is judged by ``Referer``, which must have this request's scheme and
    host. Neither header: ``False``. A ``GET`` link from another site is sent
    with the session cookie (``SameSite=Lax``), and Django's CSRF protection
    does not cover ``GET``.
    """
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    referer = request.headers.get("Referer")
    if not referer:
        return False
    parsed = urlsplit(referer)
    return parsed.scheme == request.scheme and parsed.netloc == request.get_host()


class TenantSwitchListFilter(admin.SimpleListFilter):
    """Changelist filter that renders the cross-tenant admin's tenant switcher.

    The dropdown lists every tenant from ``RLS_TENANTS["TENANT_MODEL"]`` plus an
    "All" entry (for a ``MultiTenantUser``, only the tenants they may select).
    Selecting an option reloads the changelist with
    ``?<param>=<tenant-pk>`` (or ``?<param>=__all__``); the owning
    :class:`RLSTenantModelAdmin` persists that choice to the session and applies
    the actual scoping by activating the matching context. The filter's own
    :meth:`queryset` is therefore a **no-op** -- it exists to render the control
    and to register the query parameter so the changelist accepts it.

    Only attached for users who may switch tenants (see
    :meth:`RLSTenantModelAdmin.get_list_filter`).
    """

    title = "tenant"
    parameter_name = "rls_tenant"

    def __init__(
        self,
        request: HttpRequest,
        params: dict[str, list[str]],
        model: type[Model],
        model_admin: admin.ModelAdmin[Any],
    ) -> None:
        # Remember the persisted selection so the dropdown reflects the tenant
        # currently in effect even on a paramless changelist load (the owning
        # admin syncs the session before the filter is built). A
        # MultiTenantUser's selection is the site's; with none, the request
        # runs in their default tenant.
        user: Any = request.user
        self._member = _selects_tenants(user)
        self._session_value: str | None
        if self._member:
            session_key = rls_tenants_config.TENANT_SESSION_KEY
            selected = None if session_key is None else request.session.get(session_key)
            default = user.rls_tenant_id
            self._session_value = selected or (None if default is None else str(default))
        else:
            session_key = getattr(model_admin, "rls_session_key", _DEFAULT_SESSION_KEY)
            self._session_value = request.session.get(session_key)
        super().__init__(request, params, model, model_admin)

    def lookups(
        self,
        request: HttpRequest,
        model_admin: admin.ModelAdmin[Any],  # noqa: ARG002  -- required by the base signature
    ) -> list[tuple[str, str]]:
        """Return ``(pk, label)`` pairs for every tenant the user may switch to.

        Every tenant for a tenant admin; for a ``MultiTenantUser``, their
        ``rls_tenant_id`` and ``rls_tenant_ids``. The tenant model is global
        (not RLS-protected), so this is unaffected by the active context.
        """
        tenant_model = apps.get_model(rls_tenants_config.TENANT_MODEL)
        tenants = tenant_model._default_manager.all()  # noqa: SLF001
        if self._member:
            tenants = tenants.filter(pk__in=_selectable(cast("Any", request.user)))
        return [(str(tenant.pk), str(tenant)) for tenant in tenants]

    def value(self) -> str | None:
        """Return the selected tenant id.

        The query parameter wins; otherwise fall back to the session-persisted
        selection so the dropdown still shows the active tenant on add/change
        pages that carry no query string (for a ``MultiTenantUser`` without
        one, their default tenant; ``__all__``, which clears their selection,
        shows the default too, since they have no "All" entry).
        """
        selected = super().value()
        if self._member and selected == _ALL_TENANTS:
            selected = None
        return selected or self._session_value

    def queryset(self, request: HttpRequest, queryset: Any) -> Any:  # noqa: ARG002
        """No-op: scoping is done by the owning admin's context, not here."""
        return queryset

    def choices(self, changelist: ChangeList) -> Iterator[_ListFilterChoices]:
        """Yield the "All" entry (clearing the selection) followed by each tenant.

        A ``MultiTenantUser`` gets no "All" entry: they always work in one
        tenant.
        """
        current = self.value()
        if not self._member:
            yield {
                "selected": current is None or current == _ALL_TENANTS,
                "query_string": changelist.get_query_string({self.parameter_name: _ALL_TENANTS}),
                "display": "All",
            }
        for lookup, title in self.lookup_choices:
            yield {
                "selected": current == str(lookup),
                "query_string": changelist.get_query_string({self.parameter_name: lookup}),
                "display": title,
            }


def _audit_rejected_choices(form: ModelForm[Any], tenant_id: int | str, user_id: Any) -> None:
    """Log the foreign key choices ``form`` rejected that are rows of another tenant.

    A ``ModelChoiceField`` scoped to the tenant reports another tenant's row
    as ``invalid_choice``, and the ``ModelForm`` then leaves the field out of
    ``full_clean()``, so the model's own check never sees it.
    """
    from django_rls_tenants.tenants.models import (  # noqa: PLC0415  -- models import the app registry
        _audit_rejected_values,
        _checked_foreign_keys,
    )

    model = form._meta.model  # noqa: SLF001
    checked = _checked_foreign_keys(model)
    if checked is None:
        return
    by_name = {field.name: field for field in checked[1]}
    values: dict[Any, Any] = {}
    for name, errors in form.errors.as_data().items():
        field = by_name.get(name)
        if (
            field is None
            or not isinstance(form.fields.get(name), ModelChoiceField)
            or not any(error.code == "invalid_choice" for error in errors)
        ):
            continue
        values[field] = form[name].data
    if values:
        _audit_rejected_values(
            model,
            tenant_id,
            values,
            instance=form.instance,
            source="admin",
            user_id=user_id,
        )


class RLSTenantModelAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """``ModelAdmin`` mixin that scopes every admin DB operation to the RLS context.

    Register it like any ``ModelAdmin``::

        from django.contrib import admin
        from django_rls_tenants import RLSTenantModelAdmin
        from myapp.models import Order

        @admin.register(Order)
        class OrderAdmin(RLSTenantModelAdmin):
            list_display = ("product", "amount")

    ``request.user`` must satisfy the
    :class:`~django_rls_tenants.tenants.types.TenantUser` protocol
    (``is_tenant_admin`` / ``rls_tenant_id``) -- the same contract the middleware
    and ``for_user()`` rely on.

    Attributes:
        rls_allow_tenant_switch: Give cross-tenant admins, and users who may
            select one of several tenants (``MultiTenantUser``), the tenant
            switcher. When ``False``, such admins always operate in
            ``admin_context()`` (all tenants), and such users in the tenant
            their request selects elsewhere. Defaults to ``True``.
        rls_session_key: Session key holding a tenant admin's switcher selection.
            Defaults to ``"rls_admin_tenant"``. A ``MultiTenantUser``'s is the
            site's, under ``RLS_TENANTS["TENANT_SESSION_KEY"]``.
        rls_tenant_query_param: Query parameter the switcher uses. Defaults to
            ``"rls_tenant"`` (matches :class:`TenantSwitchListFilter`).
        rls_deny_without_tenant: When ``True`` (default), a non-admin user with no
            tenant raises ``PermissionDenied`` instead of running unscoped.
    """

    rls_allow_tenant_switch: bool = True
    rls_session_key: str = _DEFAULT_SESSION_KEY
    rls_tenant_query_param: str = "rls_tenant"
    rls_deny_without_tenant: bool = True

    # ---- Context resolution -------------------------------------------------

    def _effective_tenant_id(self, request: HttpRequest) -> int | str | None:
        """Return the tenant the current request should be scoped to.

        - Cross-tenant admin: the switcher selection (``None`` means "all").
        - :class:`~django_rls_tenants.tenants.types.MultiTenantUser`: the
          tenant the request selects (the ``RLS_TENANTS["TENANT_HEADER"]``
          header, then the session's selection, which the switcher writes),
          else their ``rls_tenant_id``.
        - Scoped user: their own ``rls_tenant_id``.

        ``None`` means "no specific tenant"; :meth:`_rls_context` decides whether
        that is admin access, a denial, or an unscoped fall-through.

        Raises:
            PermissionDenied: If a ``MultiTenantUser``'s request selects a
                tenant that is not one of theirs.
        """
        user: Any = request.user
        if user.is_tenant_admin:
            if not self.rls_allow_tenant_switch:
                return None
            selected: int | str | None = request.session.get(self.rls_session_key)
            return selected
        if _selects_tenants(user):
            member_tenant: int | str | None = _request_tenant_id(request, user, source="admin")
            return member_tenant
        tenant_id: int | str | None = user.rls_tenant_id
        return tenant_id

    def _rls_context(self, request: HttpRequest) -> AbstractContextManager[None]:
        """Return the context manager to wrap a view in, or deny the request.

        Raises:
            PermissionDenied: If ``rls_deny_without_tenant`` is set and a non-admin
                user has no tenant -- fail-closed rather than silent admin access.
        """
        user: Any = request.user
        tenant_id = self._effective_tenant_id(request)
        if tenant_id is not None:
            return tenant_context(tenant_id)
        if user.is_tenant_admin:
            return admin_context()
        if self.rls_deny_without_tenant:
            audit.denied("admin", "user_without_tenant", user_id=audit.user_id_of(user))
            msg = (
                f"{type(user).__name__} is not a tenant admin and has no tenant assigned, "
                f"so this admin cannot determine which tenant's data to show."
            )
            raise PermissionDenied(f"{msg} Hint: {HINT_USER_NO_TENANT}")
        return nullcontext()

    def _rls_view_context(self, request: HttpRequest) -> AbstractContextManager[None]:
        """Return :meth:`_rls_context`, its audit records attributed to the admin and the user."""
        return audit.attributed(self._rls_context(request), source="admin", user=request.user)

    def _user_can_switch_tenant(self, request: HttpRequest) -> bool:
        """Return whether the switcher should be offered to this user.

        To a tenant admin, and to a
        :class:`~django_rls_tenants.tenants.types.MultiTenantUser` who may
        select a tenant other than their ``rls_tenant_id`` (while
        ``RLS_TENANTS["TENANT_SESSION_KEY"]`` is set, where the switcher
        stores the selection).
        """
        user: Any = request.user
        if not self.rls_allow_tenant_switch:
            return False
        if user.is_tenant_admin:
            return True
        return (
            _selects_tenants(user)
            and rls_tenants_config.TENANT_SESSION_KEY is not None
            and bool(_other_tenants(user))
        )

    def _sync_tenant_selection(self, request: HttpRequest) -> None:
        """Persist a switcher choice from the query string into the session.

        Only acts for switch-capable users and only when the query parameter is
        present, so plain navigation (add/change/delete) preserves the selection.
        ``__all__`` (or empty) clears it; any other value is stored only after it
        is confirmed to be a real tenant, so a stale or hostile id cannot wedge
        every later admin page. A selection whose tenant is *later* deleted is not
        re-validated here (no query param to act on); it simply scopes to an empty
        result set (fail-closed) until the admin picks another tenant or "All".

        A :class:`~django_rls_tenants.tenants.types.MultiTenantUser`'s choice
        is the site's selection (``switch_tenant()``): a tenant that is not
        one of theirs is a 403 and leaves the selection as it was, and
        ``__all__`` (or empty) clears it, back to their ``rls_tenant_id``.
        Because that selection steers the whole site and the switcher is a
        ``GET`` link, the choice is applied only to a same-origin navigation
        (:func:`_same_origin_navigation`); a link from another site is a 403.

        Raises:
            PermissionDenied: If a ``MultiTenantUser`` picks a tenant that is
                not one of theirs, or the choice comes from another site.
        """
        if not self._user_can_switch_tenant(request):
            return
        raw = request.GET.get(self.rls_tenant_query_param)
        if raw is None:
            return
        user: Any = request.user
        if not user.is_tenant_admin:
            if not _same_origin_navigation(request):
                audit.denied(
                    "admin",
                    "cross_site_selection",
                    user_id=audit.user_id_of(user),
                    selected=_shown(raw),
                    via="query",
                )
                raise PermissionDenied(_CROSS_SITE_MESSAGE)
            if raw in ("", _ALL_TENANTS):
                _clear(request, source="admin")
            else:
                _switch(request, raw, source="admin", via="query")
            return
        if raw in ("", _ALL_TENANTS):
            request.session.pop(self.rls_session_key, None)
        elif self._is_known_tenant(raw):
            request.session[self.rls_session_key] = raw
        else:
            request.session.pop(self.rls_session_key, None)

    def _is_known_tenant(self, raw: str) -> bool:
        """Return whether ``raw`` is the pk of an existing tenant."""
        tenant_model = apps.get_model(rls_tenants_config.TENANT_MODEL)
        try:
            return bool(tenant_model._default_manager.filter(pk=raw).exists())  # noqa: SLF001
        except (ValueError, TypeError, ValidationError):
            # raw is not castable to the tenant pk type (e.g. "abc" for an int pk).
            return False

    # ---- View wrappers ------------------------------------------------------
    #
    # Admin querysets are lazy, so the context must span the whole view (not just
    # get_queryset): the changelist is evaluated, forms are built, related
    # dropdowns are queried, and rows are saved all within these blocks.

    def changelist_view(
        self,
        request: HttpRequest,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Wrap the changelist in the request's RLS context."""
        self._sync_tenant_selection(request)
        with self._rls_view_context(request):
            return super().changelist_view(request, extra_context)

    def add_view(
        self,
        request: HttpRequest,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Wrap the add view in the request's RLS context."""
        self._sync_tenant_selection(request)
        with self._rls_view_context(request):
            return super().add_view(request, form_url, extra_context)

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Wrap the change view in the request's RLS context."""
        self._sync_tenant_selection(request)
        with self._rls_view_context(request):
            return super().change_view(request, object_id, form_url, extra_context)

    def delete_view(
        self,
        request: HttpRequest,
        object_id: str,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Wrap the delete view in the request's RLS context."""
        self._sync_tenant_selection(request)
        with self._rls_view_context(request):
            return super().delete_view(request, object_id, extra_context)

    def history_view(
        self,
        request: HttpRequest,
        object_id: str,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Wrap the history view in the request's RLS context."""
        self._sync_tenant_selection(request)
        with self._rls_view_context(request):
            return super().history_view(request, object_id, extra_context)

    # ---- Form / save hooks --------------------------------------------------
    #
    # ``get_queryset`` is intentionally **not** overridden: inside the wrapped
    # views above the context is active, so ``RLSManager.get_queryset`` already
    # scopes the changelist and related dropdowns. The autocomplete endpoint is
    # served by the admin *site* (not a wrapped view -- and on Django 6.0
    # ``autocomplete_view`` no longer lives on ``ModelAdmin`` at all), so its
    # per-request RLS context comes from :class:`RLSTenantMiddleware`; see the
    # admin guide.

    def get_exclude(
        self,
        request: HttpRequest,
        obj: Model | None = None,
    ) -> _ListOrTuple[str] | None:
        """Hide the tenant FK when the effective tenant is implicit.

        For a scoped user, or an admin who has selected a tenant, the tenant is
        known and set by :meth:`save_model`, so the field is excluded. A global
        admin with no selection keeps it visible to choose a tenant explicitly.
        """
        exclude = super().get_exclude(request, obj)
        if self._effective_tenant_id(request) is None:
            return exclude
        field = rls_tenants_config.TENANT_FK_FIELD
        existing: tuple[str, ...] = tuple(exclude) if exclude else ()
        if field in existing:
            return existing
        return (*existing, field)

    def get_fieldsets(
        self,
        request: HttpRequest,
        obj: Model | None = None,
    ) -> _FieldsetSpec:
        """Drop the tenant FK from explicit fieldsets when the tenant is implicit.

        Mirrors :meth:`get_exclude`: when the effective tenant is known the FK is
        excluded from the form, so a ``fieldsets`` layout that still names it would
        raise ``KeyError`` at render time. A global admin with no selection keeps
        the field (and its layout slot) so they can assign a tenant explicitly.
        """
        fieldsets = super().get_fieldsets(request, obj)
        if self._effective_tenant_id(request) is None:
            return fieldsets
        return _fieldsets_without(fieldsets, rls_tenants_config.TENANT_FK_FIELD)

    def get_form(
        self,
        request: HttpRequest,
        obj: Model | None = None,
        change: bool = False,
        **kwargs: Any,
    ) -> type[ModelForm[Any]]:
        """Return the form class; it stamps an implicit tenant on its instance.

        When the tenant FK is hidden (implicit tenant), the instance of an add
        form has no tenant while the form validates, so ``full_clean()`` could
        not compare the foreign keys' targets with it
        (:func:`~django_rls_tenants.tenants.models.validate_cross_tenant_fks`).
        The form sets it on its instance when it is built, as
        :meth:`save_model` does before saving.

        Its related-field dropdowns are scoped to that tenant, so they reject
        another tenant's row as an invalid choice before ``full_clean()``
        sees it; such a choice is logged as ``fk.cross_tenant_rejected``
        (:func:`_audit_rejected_choices`).
        """
        form = super().get_form(request, obj, change, **kwargs)
        tenant_id = self._effective_tenant_id(request)
        if tenant_id is None:
            return form
        stamp = self._stamp_tenant
        selected: int | str = tenant_id  # narrowed for the class body
        user_id = audit.user_id_of(request.user)

        class TenantStampedForm(form):  # type: ignore[valid-type,misc]  # a form class built at runtime
            def __init__(self, *args: Any, **form_kwargs: Any) -> None:
                super().__init__(*args, **form_kwargs)
                stamp(self.instance, selected)

            def full_clean(self) -> None:
                super().full_clean()
                if self.is_bound and self.errors:
                    _audit_rejected_choices(self, selected, user_id)

        TenantStampedForm.__name__ = TenantStampedForm.__qualname__ = form.__name__
        return TenantStampedForm

    def save_model(
        self,
        request: HttpRequest,
        obj: Model,
        form: Any,
        change: Any,
    ) -> None:
        """Stamp the effective tenant onto the object before saving.

        When the tenant FK is hidden (implicit tenant) the form never sets it, so
        it is assigned here. When the effective tenant is ``None`` (global admin
        viewing all) the field is visible and the admin's own choice stands.
        """
        tenant_id = self._effective_tenant_id(request)
        if tenant_id is not None:
            self._stamp_tenant(obj, tenant_id)
        super().save_model(request, obj, form, change)

    def _stamp_tenant(self, obj: Model, tenant_id: int | str) -> None:
        """Set ``obj``'s tenant FK to ``tenant_id``, converted to the tenant pk's type."""
        # The switcher persists its selection as a session string; normalise it
        # to the tenant pk's Python type (int/UUID) via the FK's target field so
        # pre_save signal handlers see the column's real type, not a bare str.
        fk = self.model._meta.get_field(rls_tenants_config.TENANT_FK_FIELD)  # noqa: SLF001
        target = fk.target_field  # the Tenant pk field the FK points at
        setattr(obj, fk.attname, target.to_python(tenant_id))

    def get_list_filter(self, request: HttpRequest) -> list[Any]:
        """Prepend the tenant switcher for switch-capable admins."""
        list_filter = list(super().get_list_filter(request))
        if not self._user_can_switch_tenant(request):
            return list_filter
        return [self._tenant_switch_filter_class, *list_filter]

    # ---- Internals ----------------------------------------------------------

    @cached_property
    def _tenant_switch_filter_class(self) -> type[TenantSwitchListFilter]:
        """The switcher filter class, bound to the configured query parameter.

        Cached: a ``ModelAdmin`` instance is built once at registration and
        ``rls_tenant_query_param`` never varies per request, so the (possibly
        synthesised) subclass is created at most once, not on every changelist.
        """
        if self.rls_tenant_query_param == TenantSwitchListFilter.parameter_name:
            return TenantSwitchListFilter
        return type(
            "TenantSwitchListFilter",
            (TenantSwitchListFilter,),
            {"parameter_name": self.rls_tenant_query_param},
        )
