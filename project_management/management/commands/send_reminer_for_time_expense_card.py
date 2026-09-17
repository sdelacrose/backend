import re
import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.template import loader
from dateutil.relativedelta import relativedelta
from django.core.mail import send_mail
from django.conf import settings

from ecpmpapp.utils import get_user_active_solution, get_frontend_url_for_solution
from project_management.models import TimeCard, ExpenseCard
from ecpmpapp.constants import StatusType, ENTERPRISE_SOLUTION
from project_management.utils import send_pending_approval_reminder_email


class Command(BaseCommand):
    help = 'Send reminder emails for upcoming milestones and update their statuses'

    def handle(self, *args, **options):
        time_cards = TimeCard.objects.filter(status=StatusType.PENDING.value)
        for time_card in time_cards:
            solution = self.get_solution_from_card(time_card)
            frontend_url = get_frontend_url_for_solution(solution)
            if time_card.project:
                project_owner = time_card.project.user
                if not project_owner.is_freelancer:
                    send_pending_approval_reminder_email(project_owner.company.admin_user, "Time Card", time_card, frontend_url)
            elif time_card.created_by and time_card.created_by.staff and time_card.created_by.staff.supervisor:
                send_pending_approval_reminder_email(time_card.created_by.staff.supervisor.user, "Time Card", time_card, frontend_url)
        
        expense_cards = ExpenseCard.objects.filter(status=StatusType.PENDING.value)
        for expense_card in expense_cards:
            solution = self.get_solution_from_card(expense_card)
            frontend_url = get_frontend_url_for_solution(solution)
            if expense_card.project:
                project_owner = expense_card.project.user
                if not project_owner.is_freelancer:
                    send_pending_approval_reminder_email(project_owner.company.admin_user, "Expense Card", expense_card, frontend_url)
            elif expense_card.created_by and expense_card.created_by.staff and expense_card.created_by.staff.supervisor:
                send_pending_approval_reminder_email(expense_card.created_by.staff.supervisor.user, "Expense Card", expense_card, frontend_url)

        print(self.style.SUCCESS(f"Reminder emails sent for {len(time_cards)} time cards and {len(expense_cards)} expense cards."))

    def get_solution_from_card(self, card):
        if card.project:
            return card.project.solution
        
        if (card.company_profile or card.freelancer) and card.created_by:
            return get_user_active_solution(card.created_by.id)
        
        return ENTERPRISE_SOLUTION
