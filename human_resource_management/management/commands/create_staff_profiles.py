from django.core.management.base import BaseCommand
from django.db import transaction
from models.models import User, Staff
from ecpmpapp.constants import UserType

class Command(BaseCommand):
    help = "Create Staff profiles for users who don't have one, making freelancers and admins their own supervisors."

    def handle(self, *args, **kwargs):
        users_without_staff = User.objects.filter(staff__isnull=True)

        if not users_without_staff.exists():
            self.stdout.write(self.style.SUCCESS("All users already have staff profiles."))
            return

        staff_profiles = []
        user_to_staff = {}  # Dictionary to map users to their newly created staff profiles

        # Step 1: Create Staff profiles for all users first (without assigning supervisors)
        for user in users_without_staff:
            staff_profile = Staff(
                user=user,
                supervisor=None,  # Temporarily set to None, will be updated later
                salary=0.00,
                productivity_percentage=0,
                annual_leaves_allowed=12,
                weekly_hours=40,
            )
            staff_profiles.append(staff_profile)
            user_to_staff[user] = staff_profile  # Store reference for later use

        # Bulk create all staff profiles first
        with transaction.atomic():
            created_staff_profiles = Staff.objects.bulk_create(staff_profiles)

        # Step 2: Assign supervisors now that Staff instances exist
        staff_updates = []
        for staff in created_staff_profiles:
            user = staff.user
            if user.is_freelancer:
                staff.supervisor = staff  # Freelancers supervise themselves
            elif user.is_company_admin:
                staff.supervisor = user_to_staff.get(user.company.admin_user)
            else:
                admin_user = user.company.admin_user if hasattr(user, UserType.COMPANY.value) and user.company else None
                staff.supervisor = user_to_staff.get(admin_user)

            staff_updates.append(staff)

        # Bulk update staff profiles with correct supervisors
        with transaction.atomic():
            Staff.objects.bulk_update(staff_updates, ["supervisor"])

        self.stdout.write(
            self.style.SUCCESS(f"Successfully created and updated {len(created_staff_profiles)} staff profiles.")
        )
