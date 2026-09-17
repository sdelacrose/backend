from django.core.management.base import BaseCommand
from chat.models import ChatRoom
from models.models import CompanyProfile
from project_management.models.project import Project

class Command(BaseCommand):
    help = 'Creates chatrooms for all existing companies and projects'

    def handle(self, *args, **kwargs):
        companies = CompanyProfile.objects.all()
        for company in companies:
            chatroom, created = ChatRoom.objects.get_or_create(
                company=company,
                name=company.company_name,
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created company-wide chatroom for {company.company_name}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"Chatroom already exists for {company.company_name}"))

        projects = Project.objects.all()
        for project in projects:
            chatroom, created = ChatRoom.objects.get_or_create(
                project=project,
                name=project.project_name,
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created project chatroom for {project.project_name}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"Chatroom already exists for {project.project_name}"))
