"""LINKS-RECETTE-05AM: first user, company, roles and licence of the shared "recette" environment.

Run by hand (never by the entry point), once the Auth0 account of the first tester exists:

    python manage.py bootstrap_recette --email ... --auth0-sub 'auth0|...' --first-name ... --last-name ...

Idempotent and all-or-nothing (one transaction). It only runs when RECETTE_MODE is on, creates nobody
from a token, grants no Django superuser right and never revives or extends a licence: a revoked or
expired licence stops the command, to be handled by hand. Permissions and roles come from the
versioned ecpmpapp/recette_bootstrap_catalogue.json (the back-office's own catalogue and "owner" roles).
Roles are filled only when created, so a right removed later by hand is not granted back.
"""
import json
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

PRODUCT_NAME = "Energy Consultant Solution"
LICENCE_MONTHS_AS_DAYS = 365
CATALOGUE_PATH = Path(__file__).resolve().parents[3] / "ecpmpapp" / "recette_bootstrap_catalogue.json"
AUTH0_SUB_RE = re.compile(r"^[A-Za-z0-9_-]+\|[A-Za-z0-9._-]+$")


class Command(BaseCommand):
    help = "Create (or find) the recette company, its first user, owner roles and a test licence."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--auth0-sub", required=True)
        parser.add_argument("--first-name", required=True)
        parser.add_argument("--last-name", required=True)
        parser.add_argument("--company-name", default="Sharemundo Recette")

    def handle(self, *args, **options):
        if getattr(settings, "RECETTE_MODE", False) is not True:
            raise CommandError("bootstrap_recette only runs with RECETTE_MODE enabled.")
        email = (options["email"] or "").strip().lower()
        sub = (options["auth0_sub"] or "").strip()
        first_name = (options["first_name"] or "").strip()
        last_name = (options["last_name"] or "").strip()
        company_name = (options["company_name"] or "").strip()
        self.validate(email, sub, first_name, last_name, company_name)

        catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        with transaction.atomic():
            user, user_created = self.get_or_create_user(email, sub, first_name, last_name)
            company, company_created = self.get_or_create_company(user, company_name)
            self.ensure_company_data(user, company)
            self.ensure_roles(user, company, catalogue)
            licence_created = self.ensure_licence(user, company)
        self.stdout.write(
            f"bootstrap_recette (catalogue {catalogue['version']}): user {'created' if user_created else 'found'}, "
            f"company {'created' if company_created else 'found'}, licence {'created' if licence_created else 'found'}."
        )

    @staticmethod
    def validate(email, sub, first_name, last_name, company_name):
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError("--email is not a valid address.")
        if not sub or len(sub) > 400 or not AUTH0_SUB_RE.match(sub):
            raise CommandError("--auth0-sub must look like '<provider>|<id>' (the Auth0 'sub' claim).")
        if not first_name or len(first_name) > 30 or not last_name or len(last_name) > 30:
            raise CommandError("--first-name and --last-name are required (30 characters at most).")
        if not company_name:
            raise CommandError("--company-name must not be empty.")

    def get_or_create_user(self, email, sub, first_name, last_name):
        from models.models import User

        by_email = User.objects.filter(email__iexact=email).first()
        by_sub = User.objects.filter(auth0_sub=sub).first()
        if by_email and by_sub and by_email.pk != by_sub.pk:
            raise CommandError("The e-mail and the Auth0 sub belong to two different users.")
        user = by_email or by_sub
        if user is None:
            user = User(
                email=email, first_name=first_name, last_name=last_name, user_type="company_user",
                auth0_sub=sub, is_active=True, license_allocated=True,
            )
            user.set_unusable_password()
            user.save()
            return user, True
        if user.email.lower() != email or user.auth0_sub != sub:
            raise CommandError("This user exists with a different e-mail or Auth0 sub: nothing changed.")
        if not user.is_active or user.is_deleted or user.is_permanently_deleted:
            raise CommandError("This user is inactive or deleted: it is not reactivated by the bootstrap.")
        return user, False

    def get_or_create_company(self, user, company_name):
        from models.models import CompanyProfile

        company = user.company or CompanyProfile.objects.filter(admin_user=user).first()
        if company is not None:
            if company.company_name != company_name:
                raise CommandError("This user is already linked to another company.")
            created = False
        else:
            if CompanyProfile.objects.filter(company_name=company_name).exists():
                raise CommandError("A company with this name already exists with another owner.")
            company = CompanyProfile.objects.create(
                admin_user=user, company_name=company_name, company_address="Recette", post_code="00000",
                company_town="Recette", company_country="France", company_tell_no="-", company_mob_no="-",
                company_web_address="https://recette.invalid", company_email_address="recette@links-recette.invalid",
                company_tax_reference="RECETTE", company_iban="RECETTE",
            )
            created = True
        if user.company_id != company.id:
            user.company = company
            user.save(update_fields=["company"])
        return company, created

    @staticmethod
    def ensure_company_data(user, company):
        from models.models import CompanyConfig, Staff

        # user_info reads user.staff; the Network company form needs at least one company role.
        Staff.objects.get_or_create(user=user, defaults={"salary": 0})
        CompanyConfig.objects.get_or_create(
            company_profile=company, solution=PRODUCT_NAME, defaults={"company_roles": ["Client", "Supplier"]}
        )

    @staticmethod
    def ensure_roles(user, company, catalogue):
        from models.models import Permission, Role, RolePermission, UserRole

        permissions = {}
        for key, spec in catalogue["permissions"].items():
            permissions[key], _ = Permission.objects.get_or_create(
                key=key,
                defaults={"name": spec["name"], "description": spec["description"], "module": spec["module"]},
            )
        for spec in catalogue["roles"].values():
            role, created = Role.objects.get_or_create(
                name=spec["name"], company=company, freelancer=None, scope=spec["scope"], type=spec["type"],
                defaults={"description": spec["description"], "is_default": True, "is_active": True, "created_by": user},
            )
            if created:
                RolePermission.objects.bulk_create(
                    [RolePermission(role=role, permission=permissions[key]) for key in spec["permissions"]]
                )
            if spec["scope"] == "company":  # project roles are assigned per project
                UserRole.objects.get_or_create(
                    user=user, role=role, company=company, freelancer=None, defaults={"assigned_by": user}
                )

    @staticmethod
    def ensure_licence(user, company):
        from package_license.models import License, Package, Product

        now = timezone.now()
        product, _ = Product.objects.get_or_create(
            name=PRODUCT_NAME, defaults={"description": "Recette test product", "price": 0, "status": "Active"}
        )
        package = Package.objects.filter(company_profile=company, user=user).order_by("id").first()
        if package is None:
            package = Package.objects.create(
                is_active=True, company_profile=company, user=user, status="Cancelled", is_freetrial=True,
                expires_at=now + timedelta(days=LICENCE_MONTHS_AS_DAYS),
            )
            created = True
        elif not package.is_active or package.expires_at is None or package.expires_at <= now:
            raise CommandError("The existing test package is revoked or expired: it is not revived or extended.")
        else:
            created = False
        licence = License.objects.filter(package=package, product=product, user=user).first()
        if licence is None:
            License.objects.create(
                package=package, product=product, user=user, is_active=True, license_assigned_at=now
            )
            created = True
        elif not licence.is_active:
            raise CommandError("The existing licence is revoked: it is not reactivated.")
        return created
