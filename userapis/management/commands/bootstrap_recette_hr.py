"""RELEASE-02B: HR test roles of the shared "recette" environment.

Run by hand, after `bootstrap_recette`, once the Auth0 account of the HR tester exists:

    python manage.py bootstrap_recette_hr --company-name "Sharemundo Recette" \
        --email ... --auth0-sub 'auth0|...' --first-name ... --last-name ... [--with-sst]

Creates (or finds) the HR tester as a member of the existing recette company, gives them a test licence
on the company's existing package, and assigns the "Responsable RH — Recette" role and, only with
--with-sst, the separate "Référent SST — Recette" role. Roles and permissions come from the versioned
ecpmpapp/recette_hr_roles_catalogue.json and reuse the existing Role / RolePermission / UserRole model.

Safety rules (same discipline as bootstrap_recette):
* RECETTE_MODE only; idempotent and all-or-nothing (one transaction).
* Never assigns these roles to the company owner (admin_user): sensitive HR/SST rights are never
  granted to the Company Owner automatically.
* The HR role is always assigned before the SST role: the backend resolves a user's primary
  company role as the oldest UserRole, so leave_request/staff_management checks stay on the HR role.
* Roles are filled only when created, so a right removed later by hand is not granted back.
* Never revives or extends a revoked/expired package or licence, never creates a package.
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from userapis.management.commands.bootstrap_recette import PRODUCT_NAME, Command as BootstrapCommand

CATALOGUE_PATH = Path(__file__).resolve().parents[3] / "ecpmpapp" / "recette_hr_roles_catalogue.json"


class Command(BaseCommand):
    help = "Create (or find) the recette HR tester and assign the recette HR (and optionally SST) roles."

    def add_arguments(self, parser):
        parser.add_argument("--company-name", default="Sharemundo Recette")
        parser.add_argument("--email", required=True)
        parser.add_argument("--auth0-sub", required=True)
        parser.add_argument("--first-name", required=True)
        parser.add_argument("--last-name", required=True)
        parser.add_argument("--with-sst", action="store_true", help="Also assign the separate SST role.")

    def handle(self, *args, **options):
        if getattr(settings, "RECETTE_MODE", False) is not True:
            raise CommandError("bootstrap_recette_hr only runs with RECETTE_MODE enabled.")
        email = (options["email"] or "").strip().lower()
        sub = (options["auth0_sub"] or "").strip()
        first_name = (options["first_name"] or "").strip()
        last_name = (options["last_name"] or "").strip()
        company_name = (options["company_name"] or "").strip()
        BootstrapCommand.validate(email, sub, first_name, last_name, company_name)

        catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        role_keys = ["hr", "sst"] if options["with_sst"] else ["hr"]
        with transaction.atomic():
            company = self.get_company(company_name)
            user, user_created = self.get_or_create_member(company, email, sub, first_name, last_name)
            assigned = self.ensure_roles(user, company, catalogue, role_keys)
            licence_created = self.ensure_licence(user, company)
        self.stdout.write(
            f"bootstrap_recette_hr (catalogue {catalogue['version']}): user {'created' if user_created else 'found'}, "
            f"roles {', '.join(assigned)}, licence {'created' if licence_created else 'found'}."
        )

    @staticmethod
    def get_company(company_name):
        from models.models import CompanyProfile

        company = CompanyProfile.objects.filter(company_name=company_name).first()
        if company is None:
            raise CommandError("Recette company not found: run bootstrap_recette first.")
        return company

    @staticmethod
    def get_or_create_member(company, email, sub, first_name, last_name):
        from models.models import Staff, User

        by_email = User.objects.filter(email__iexact=email).first()
        by_sub = User.objects.filter(auth0_sub=sub).first()
        if by_email and by_sub and by_email.pk != by_sub.pk:
            raise CommandError("The e-mail and the Auth0 sub belong to two different users.")
        user = by_email or by_sub
        created = False
        if user is None:
            user = User(
                email=email, first_name=first_name, last_name=last_name, user_type="company_user",
                auth0_sub=sub, is_active=True, license_allocated=True, company=company,
            )
            user.set_unusable_password()
            user.save()
            created = True
        else:
            if user.email.lower() != email or user.auth0_sub != sub:
                raise CommandError("This user exists with a different e-mail or Auth0 sub: nothing changed.")
            if not user.is_active or user.is_deleted or user.is_permanently_deleted:
                raise CommandError("This user is inactive or deleted: it is not reactivated.")
            if user.company_id != company.id:
                raise CommandError("This user belongs to another company: nothing changed.")
        if company.admin_user_id == user.id:
            raise CommandError("The HR/SST recette roles are never assigned to the company owner.")
        Staff.objects.get_or_create(user=user, defaults={"salary": 0})
        return user, created

    @staticmethod
    def ensure_roles(user, company, catalogue, role_keys):
        from models.models import Permission, Role, RolePermission, UserRole

        permissions = {}
        for key, spec in catalogue["permissions"].items():
            permissions[key], _ = Permission.objects.get_or_create(
                key=key,
                defaults={"name": spec["name"], "description": spec["description"], "module": spec["module"]},
            )
        assigned = []
        for role_key in role_keys:  # "hr" always first (oldest UserRole = backend primary role)
            spec = catalogue["roles"][role_key]
            role, created = Role.objects.get_or_create(
                name=spec["name"], company=company, freelancer=None, scope=spec["scope"], type=spec["type"],
                defaults={"description": spec["description"], "is_default": False, "is_active": True,
                          "created_by": company.admin_user},
            )
            if created:
                RolePermission.objects.bulk_create(
                    [RolePermission(role=role, permission=permissions[key]) for key in spec["permissions"]]
                )
            UserRole.objects.get_or_create(
                user=user, role=role, company=company, freelancer=None, defaults={"assigned_by": company.admin_user}
            )
            assigned.append(spec["name"])
        return assigned

    @staticmethod
    def ensure_licence(user, company):
        from package_license.models import License, Package, Product

        now = timezone.now()
        product = Product.objects.filter(name=PRODUCT_NAME).first()
        package = (
            Package.objects.filter(company_profile=company, is_active=True, expires_at__gt=now).order_by("id").first()
        )
        if product is None or package is None:
            raise CommandError("No active recette package for this company: run bootstrap_recette first.")
        licence = License.objects.filter(package=package, product=product, user=user).first()
        if licence is None:
            License.objects.create(package=package, product=product, user=user, is_active=True, license_assigned_at=now)
            return True
        if not licence.is_active:
            raise CommandError("The existing licence is revoked: it is not reactivated.")
        return False
