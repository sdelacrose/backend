import json
import decimal
from datetime import date
from django.core.management.base import BaseCommand

from models.models import User, CompanyProfile
from companyprofileapis.serializers import UserSerializer
from project_management.models import *


class Command(BaseCommand):
    help = 'Extract specific user data by email'

    def add_arguments(self, parser):
        parser.add_argument('email', type=str, help='Email of the user to extract data for')

    def handle(self, *args, **kwargs):
        from project_management.serializers import (
            ProjectSerializer, MilestoneSerializer, ActionItemSerializer,
            FolderSerializer, InvoiceSerializer, ObjectAssessmentSerializer,
            ContactUserSerializer, ContactCompanySerializer, ProjectPhaseSerializer,
            ProjectFundingSerializer, SiteReportSerializer, TimeCardSerializer,
            ExpenseCardSerializer
        )

        email = kwargs['email']
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f'User with email {email} does not exist'))
            return

        user_data = UserSerializer(user).data
        projects = Project.objects.filter(user__company_id=user.company.id)
        projects_data = {}
        for project in projects:
            project_data = {}
            project_data['project'] = ProjectSerializer(project).data
            milestones = []
            for milestone in project.milestones.all():
                milestone_data = MilestoneSerializer(milestone).data
                action_items = []
                for action_item in milestone.action_items.all():
                    action_items.append(ActionItemSerializer(action_item).data)
                milestones.append({
                    'milestone': milestone_data,
                    'action_items': action_items
                })
            project_folder = Folder.objects.get(name=f"{project.id}_root_folder")
            project_documents = FolderSerializer(project_folder).data
            billing_invoices = InvoiceSerializer(project.billing_invoices.all(), many=True).data
            object_assessments = ObjectAssessmentSerializer(project.object_assessments.all(), many=True).data
            phases = ProjectPhaseSerializer(project.project_phases.all(), many=True).data
            fundings = ProjectFundingSerializer(project.fundings.all(), many=True).data
            site_reports = SiteReportSerializer(project.site_reports.all(), many=True).data
            time_cards = TimeCardSerializer(project.time_cards.all(), many=True).data
            expense_cards = ExpenseCardSerializer(project.expense_cards.all(), many=True).data

            project_data['milestones'] = milestones
            project_data['project_documents'] = project_documents
            project_data['billing_invoices'] = billing_invoices
            project_data['object_assessments'] = object_assessments
            project_data['phases'] = phases
            project_data['fundings'] = fundings
            project_data['site_reports'] = site_reports
            project_data['time_cards'] = time_cards
            project_data['expense_cards'] = expense_cards
            projects_data[project.project_name] = project_data

        company_data = {}
        company = user.company
        company_folder = Folder.objects.get(name=f"{company.id}_company_root_folder")
        company_documents = FolderSerializer(company_folder).data
        company_ac = ActionItemSerializer(company.action_items.all(), many=True).data
        contact_users = ContactUserSerializer(company.contact_users.all(), many=True).data
        contact_companies = ContactCompanySerializer(company.contact_companies.all(), many=True).data
        company_invoices = InvoiceSerializer(company.billing_invoices.all(), many=True).data
        company_time_cards = TimeCardSerializer(company.time_cards.all(), many=True).data
        company_expense_cards = ExpenseCardSerializer(company.expense_cards.all(), many=True).data

        company_data['company_documents'] = company_documents
        company_data['company_ac'] = company_ac
        company_data['contact_users'] = contact_users
        company_data['contact_companies'] = contact_companies
        company_data['company_invoices'] = company_invoices
        company_data['company_time_cards'] = company_time_cards
        company_data['company_expense_cards'] = company_expense_cards
        company_data['projects'] = projects_data
        company_data['user'] = user_data

        class CustomEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, decimal.Decimal):
                    return str(obj)
                if isinstance(obj, date):
                    return obj.isoformat()
                return super().default(obj)

        output_file = f'{company.company_name}.json'
        with open(output_file, 'w') as json_file:
            json.dump(company_data, json_file, indent=4, cls=CustomEncoder)
        
        self.stdout.write(self.style.SUCCESS(f'Successfully exported data to {output_file}'))
          
