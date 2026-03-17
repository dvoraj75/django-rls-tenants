from django.core.management.base import BaseCommand
from django_rls_tenants import admin_context, tenant_context

TENANTS = [
    {"name": "Acme Corp", "slug": "acme"},
    {"name": "Globex Inc", "slug": "globex"},
    {"name": "Initech", "slug": "initech"},
]

USERS = [
    {"email": "alice@acme.com", "tenant_slug": "acme", "is_admin": False},
    {"email": "bob@globex.com", "tenant_slug": "globex", "is_admin": False},
    {"email": "carol@initech.com", "tenant_slug": "initech", "is_admin": False},
    {"email": "admin@example.com", "tenant_slug": None, "is_admin": True},
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

    def handle(self, **options):
        from accounts.models import User
        from notes.models import Category, Note
        from tenants.models import Tenant

        if User.objects.filter(email="admin@example.com").exists():
            self.stdout.write("Demo data already exists, skipping.")
            return

        with admin_context():
            # Create tenants
            tenants = {}
            for t in TENANTS:
                tenants[t["slug"]] = Tenant.objects.create(**t)
                self.stdout.write(f"  Created tenant: {t['name']}")

            # Create users
            for u in USERS:
                tenant = tenants.get(u["tenant_slug"])
                User.objects.create_user(
                    username=u["email"].split("@")[0],
                    email=u["email"],
                    password="demo1234" if not u["is_admin"] else "admin1234",
                    tenant=tenant,
                    is_tenant_admin=u["is_admin"],
                    is_staff=u["is_admin"],
                    is_superuser=u["is_admin"],
                )
                self.stdout.write(f"  Created user: {u['email']}")

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
        self.stdout.write("  admin / admin1234  (Admin -- sees all)")
