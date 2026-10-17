from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand
from django_rls_tenants import admin_context, tenant_context

TENANTS = [
    {"name": "Acme Corp", "slug": "acme"},
    {"name": "Globex Inc", "slug": "globex"},
    {"name": "Initech", "slug": "initech"},
]

# Demo accounts. ``tenant`` and ``is_tenant_admin`` drive the RLS role (the
# ``TenantUser`` protocol); ``is_staff`` / ``is_superuser`` are plain Django
# admin permissions and are independent of it.
USERS = [
    # Tenant users: see only their own tenant's data; no admin access.
    {
        "username": "alice",
        "email": "alice@acme.com",
        "password": "demo1234",
        "tenant_slug": "acme",
        "is_tenant_admin": False,
        "is_staff": False,
        "is_superuser": False,
    },
    {
        "username": "bob",
        "email": "bob@globex.com",
        "password": "demo1234",
        "tenant_slug": "globex",
        "is_tenant_admin": False,
        "is_staff": False,
        "is_superuser": False,
    },
    {
        "username": "carol",
        "email": "carol@initech.com",
        "password": "demo1234",
        "tenant_slug": "initech",
        "is_tenant_admin": False,
        "is_staff": False,
        "is_superuser": False,
    },
    # Tenant-scoped staff: may log into /admin/ (is_staff + permissions on the
    # notes app) but is NOT a tenant admin, so RLSTenantModelAdmin scopes the
    # changelists to Acme and hides + auto-assigns the tenant FK on the forms.
    {
        "username": "staff",
        "email": "staff@acme.com",
        "password": "staff1234",
        "tenant_slug": "acme",
        "is_tenant_admin": False,
        "is_staff": True,
        "is_superuser": False,
    },
    # Consultant in two tenants (a MultiTenantUser, see accounts/models.py):
    # works in Acme unless they pick Globex with the switcher in the page header
    # (switch_tenant()) or the X-Tenant header of an API request.
    {
        "username": "dave",
        "email": "dave@consultants.example",
        "password": "demo1234",
        "tenant_slug": "acme",
        "member_of": ["globex"],
        "is_tenant_admin": False,
        "is_staff": False,
        "is_superuser": False,
    },
    # Cross-tenant admin: sees every tenant and gets the admin tenant switcher.
    {
        "username": "admin",
        "email": "admin@example.com",
        "password": "admin1234",
        "tenant_slug": None,
        "is_tenant_admin": True,
        "is_staff": True,
        "is_superuser": True,
    },
]

# Per-tenant categories
CATEGORIES = {
    "acme": ["Engineering", "Product", "Hiring"],
    "globex": ["Product", "Investors", "Security"],
    "initech": ["Operations", "HR", "Engineering"],
}

# Notes now reference categories (by name within the tenant)
NOTES = {
    "acme": [
        ("Q3 Planning", "Review roadmap priorities for next quarter.", "Product"),
        ("Customer Feedback", "Aggregate NPS results from the latest survey.", "Product"),
        ("Bug Triage", "Go through P1 tickets and assign owners.", "Engineering"),
        ("Hiring Update", "Three new candidates in the pipeline for backend role.", "Hiring"),
        ("Deployment Checklist", "Verify staging env before Friday release.", "Engineering"),
    ],
    "globex": [
        ("Product Launch", "Finalize landing page copy and pricing table.", "Product"),
        ("Investor Meeting", "Prepare slides for Series B update.", "Investors"),
        ("Security Audit", "Schedule pen test with external vendor.", "Security"),
        ("API Docs", "Update OpenAPI spec for v2 endpoints.", None),
    ],
    "initech": [
        ("TPS Reports", "New cover sheet format is mandatory starting Monday.", "Operations"),
        ("Office Supplies", "Order more red staplers for the 4th floor.", "Operations"),
        ("Migration Plan", "Move legacy system to the new platform by Q4.", "Engineering"),
        ("Team Lunch", "Book restaurant for Friday team lunch.", None),
        ("Onboarding", "Update onboarding docs for new hires.", "HR"),
        ("Performance Reviews", "Submit self-reviews by end of month.", "HR"),
    ],
}


class Command(BaseCommand):
    help = "Seed the database with demo tenants, users, categories, and notes."

    def add_arguments(self, parser):
        parser.add_argument("--no-input", action="store_true")

    def _create_user(self, user_data, tenant, tenants):
        from accounts.models import Membership, User

        user = User.objects.create_user(
            username=user_data["username"],
            email=user_data["email"],
            password=user_data["password"],
            tenant=tenant,
            is_tenant_admin=user_data["is_tenant_admin"],
            is_staff=user_data["is_staff"],
            is_superuser=user_data["is_superuser"],
        )
        if user.is_staff and not user.is_superuser:
            # Only grant access to the RLS-protected app. User and Tenant admins
            # are global and must not be accessible to tenant-scoped staff.
            user.user_permissions.set(Permission.objects.filter(content_type__app_label="notes"))
        for slug in user_data.get("member_of", ()):
            Membership.objects.create(user=user, tenant=tenants[slug])
        self.stdout.write(f"  Created user: {user_data['email']}")

    def handle(self, **options):
        from accounts.models import User
        from notes.models import Category, Note
        from tenants.models import Tenant

        if User.objects.filter(email="admin@example.com").exists():
            # Upgrade older demo volumes without deleting or replacing their data:
            # add the accounts introduced since (staff, dave) if they are missing.
            tenants = {tenant.slug: tenant for tenant in Tenant.objects.all()}
            for user_data in USERS:
                if user_data["username"] not in ("staff", "dave"):
                    continue
                slugs = [user_data["tenant_slug"], *user_data.get("member_of", ())]
                if (
                    all(slug in tenants for slug in slugs)
                    and not User.objects.filter(username=user_data["username"]).exists()
                ):
                    self._create_user(user_data, tenants[user_data["tenant_slug"]], tenants)
            self.stdout.write("Demo data already exists, preserving existing accounts and notes.")
            return

        with admin_context():
            # Create tenants
            tenants = {}
            for t in TENANTS:
                tenants[t["slug"]] = Tenant.objects.create(**t)
                self.stdout.write(f"  Created tenant: {t['name']}")

            # Create users
            for u in USERS:
                self._create_user(u, tenants.get(u["tenant_slug"]), tenants)

            # Create categories (per-tenant, RLS-protected)
            cat_objects = {}  # (slug, cat_name) -> Category instance
            for slug, cat_names in CATEGORIES.items():
                tenant = tenants[slug]
                for cat_name in cat_names:
                    cat = Category.objects.create(name=cat_name, tenant=tenant)
                    cat_objects[(slug, cat_name)] = cat
                self.stdout.write(f"  Created {len(cat_names)} categories for {tenant.name}")

            # Create notes with optional category references
            for slug, notes in NOTES.items():
                tenant = tenants[slug]
                for title, content, cat_name in notes:
                    category = cat_objects.get((slug, cat_name)) if cat_name else None
                    Note.objects.create(
                        title=title,
                        content=content,
                        tenant=tenant,
                        category=category,
                    )
                self.stdout.write(f"  Created {len(notes)} notes for {tenant.name}")

        # ── Verify tenant isolation using tenant_context() ──────────
        #
        # Demonstrates programmatic scoping outside of HTTP requests.
        # Each tenant should only see their own data.
        self.stdout.write("")
        self.stdout.write("Verifying tenant isolation with tenant_context():")
        for slug, tenant in tenants.items():
            with tenant_context(tenant.pk):
                note_count = Note.objects.count()
                cat_count = Category.objects.count()
            expected_notes = len(NOTES[slug])
            expected_cats = len(CATEGORIES[slug])
            status = (
                "OK"
                if (note_count == expected_notes and cat_count == expected_cats)
                else "MISMATCH"
            )
            self.stdout.write(
                f"  {tenant.name}: {note_count} notes, {cat_count} categories [{status}]"
            )

        self.stdout.write("")
        self.stdout.write("Demo credentials (username / password):")
        self.stdout.write("  alice / demo1234   (Acme Corp)")
        self.stdout.write("  bob   / demo1234   (Globex Inc)")
        self.stdout.write("  carol / demo1234   (Initech)")
        self.stdout.write("  staff / staff1234  (Acme Corp staff -- /admin/ scoped to Acme)")
        self.stdout.write("  dave  / demo1234   (Acme Corp, also Globex Inc -- tenant switcher)")
        self.stdout.write("  admin / admin1234  (Admin -- sees all, /admin/ tenant switcher)")
