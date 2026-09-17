from django.core.management.base import BaseCommand
from django.utils import timezone
from project_management.models import ContactUser, ContactCompany

class Command(BaseCommand):
    help = "Set deleted_at to now for records where is_deleted=True and deleted_at is NULL"

    def handle(self, *args, **kwargs):
        now = timezone.now()

        # Update ContactUser records
        contact_user_updated = ContactUser.objects.filter(is_deleted=True, deleted_at__isnull=True).update(deleted_at=now)

        # Update ContactCompany records
        contact_company_updated = ContactCompany.objects.filter(is_deleted=True, deleted_at__isnull=True).update(deleted_at=now)

        self.stdout.write(self.style.SUCCESS(
            f"Successfully updated {contact_user_updated} ContactUser records and {contact_company_updated} ContactCompany records."
        ))
