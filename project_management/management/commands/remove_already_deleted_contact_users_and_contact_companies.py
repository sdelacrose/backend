import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from project_management.models import ContactCompany, ContactUser

class Command(BaseCommand):
    help = 'Deletes ContactUser and ContactCompany marked as deleted if their delete_at date is older than 60 days'

    def handle(self, *args, **options):
        # Calculate the date 60 days ago from today
        cutoff_date = timezone.now() - datetime.timedelta(days=60)

        # Filter contacts who are marked as deleted and have a delete_at older than 60 days
        users_to_delete = ContactUser.objects.filter(is_deleted=True, deleted_at__date__lt=cutoff_date.date())
        companies_to_delete = ContactCompany.objects.filter(is_deleted=True, deleted_at__date__lt=cutoff_date.date())


        # Count the number of contacts users and companies to delete
        users_count = users_to_delete.count()
        companies_count = companies_to_delete.count()

        # Delete the contact users and companies
        users_to_delete.delete()
        companies_to_delete.delete()

        # Print the result
        self.stdout.write(self.style.SUCCESS(f'Successfully deleted {users_count} contact user(s)'))
        self.stdout.write(self.style.SUCCESS(f'Successfully deleted {companies_count} contact company(s)'))
