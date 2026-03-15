from django.core.management.base import BaseCommand
from django_rls_tenants import admin_context

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

NOTES = {
    "acme": [
        ("Q3 Planning", "Review roadmap priorities for next quarter."),
        ("Customer Feedback", "Aggregate NPS results from the latest survey."),
        ("Bug Triage", "Go through P1 tickets and assign owners."),
        ("Hiring Update", "Three new candidates in the pipeline for backend role."),
        ("Deployment Checklist", "Verify staging env before Friday release."),
    ],
    "globex": [
        ("Product Launch", "Finalize landing page copy and pricing table."),
        ("Investor Meeting", "Prepare slides for Series B update."),
        ("Security Audit", "Schedule pen test with external vendor."),
        ("API Docs", "Update OpenAPI spec for v2 endpoints."),
    ],
    "initech": [
        ("TPS Reports", "New cover sheet format is mandatory starting Monday."),
        ("Office Supplies", "Order more red staplers for the 4th floor."),
        ("Migration Plan", "Move legacy system to the new platform by Q4."),
        ("Team Lunch", "Book restaurant for Friday team lunch."),
        ("Onboarding", "Update onboarding docs for new hires."),
        ("Performance Reviews", "Submit self-reviews by end of month."),
    ],
}


class Command(BaseCommand):
    help = "Seed the database with demo tenants, users, and notes."

    def add_arguments(self, parser):
        parser.add_argument("--no-input", action="store_true")

    def handle(self, **options):
        from accounts.models import User
        from notes.models import Note
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

            # Create notes
            for slug, notes in NOTES.items():
                tenant = tenants[slug]
                for title, content in notes:
                    Note.objects.create(
                        title=title,
                        content=content,
                        tenant=tenant,
                    )
                self.stdout.write(f"  Created {len(notes)} notes for {tenant.name}")

        self.stdout.write("")
        self.stdout.write("Demo credentials:")
        self.stdout.write("  alice@acme.com    / demo1234   (Acme Corp)")
        self.stdout.write("  bob@globex.com    / demo1234   (Globex Inc)")
        self.stdout.write("  carol@initech.com / demo1234   (Initech)")
        self.stdout.write("  admin@example.com / admin1234  (Admin — sees all)")
