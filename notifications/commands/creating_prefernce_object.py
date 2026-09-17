from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from models.models import Preference


class Command(BaseCommand):
    help = 'Ensure every user has an associated Preference object'

    def handle(self, *args, **options):
        User = get_user_model()
        users = User.objects.all()
        created_count = 0

        for user in users:
            preference, created = Preference.objects.get_or_create(user=user)
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Created Preference for user: {user.email} (ID: {user.id})'
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'Done! Preferences created for {created_count} user(s).'
            )
        )
