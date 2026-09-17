from django.core.management.base import BaseCommand
from django.db import transaction
from models.models import Staff

class Command(BaseCommand):
    help = "Update weekly_hours of all staff profiles and set them to 40."

    def handle(self, *args, **kwargs):

        # Fetch all staff profiles
        staff_profiles = Staff.objects.all()

        if not staff_profiles.exists():
            self.stdout.write(self.style.WARNING("No staff profiles found to update."))
            return

        # Update weekly_hours to 40 for all staff profiles
        with transaction.atomic():
            updated_profiles = []
            for staff in staff_profiles:
                    staff.weekly_hours = 40
                    updated_profiles.append(staff)

            # Bulk update to optimize DB writes
            Staff.objects.bulk_update(updated_profiles, ['weekly_hours'])

        self.stdout.write(self.style.SUCCESS(f"Successfully updated weekly_hours to 40 for {len(updated_profiles)} staff profiles."))
