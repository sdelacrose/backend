from django.core.management.base import BaseCommand
from models.models import User


class Command(BaseCommand):
    help = "Set 'English' as the default preferred_language for all existing users without a preferred_language value."

    def handle(self, *args, **kwargs):
        # Fetch all users who don't have a preferred_language set
        users_without_language = User.objects.filter(preferred_language__isnull=True)

        # Update the preferred_language for all these users
        updated_count = users_without_language.update(preferred_language="English")

        self.stdout.write(self.style.SUCCESS(
            f"Successfully updated {updated_count} user(s) to have 'English' as their preferred language."
        ))