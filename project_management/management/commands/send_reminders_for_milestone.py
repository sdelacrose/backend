import re
import datetime
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.template import loader
from dateutil.relativedelta import relativedelta
from django.core.mail import send_mail
from django.conf import settings

from project_management.models import (
    ActionItem,
    ActionItemReminderDelivery,
    Milestone,
)
from ecpmpapp.alt_messages import (
    plain_message_action_item_reminder,
    plain_message_action_item_reminder_with_milestone,
    plain_message_action_item_reminder_with_company
)
from ecpmpapp.utils import get_frontend_url_for_solution, get_user_active_solution
from project_management.constants import (
    frontend_url_project_milestone,
    frontend_url_project_action_item,
    frontend_url_company_action_item,
)
from project_management.utils import get_user_email_preferences

class Command(BaseCommand):
    help = 'Send reminder emails for upcoming milestones and update their statuses'

    def handle(self, *args, **options):
        today = timezone.now().date()
        milestones = Milestone.objects.exclude(status__iexact="closed")
        
        for milestone in milestones:
            due_date = milestone.due_date
            reminder_settings = milestone.reminder_setting

            # Parse reminder_settings
            reminder_days = self.parse_reminder_settings(reminder_settings)

            if reminder_days is not None:
                reminder_date = due_date - relativedelta(days=reminder_days)

                if reminder_date == today:  # updated from (reminder_date <= today)
                    self.send_reminder_email(milestone)

        action_items = ActionItem.objects.exclude(status__iexact="closed")
        for action_item in action_items:
            milestone = None
            if action_item.milestone:
                milestone = action_item.milestone
            elif action_item.parent_action_item and \
                 action_item.parent_action_item.milestone:
                milestone = action_item.parent_action_item.milestone
            if milestone and milestone.project.project_status.lower() in {"on hold", "closed"}:
                continue

            due_date = action_item.target_date
            reminder_settings = action_item.reminder

            # Parse reminder_settings
            reminder_days = self.parse_reminder_settings(reminder_settings)

            if reminder_days is not None:
                reminder_date = due_date - relativedelta(days=reminder_days)

                if reminder_date == today:
                    self.send_action_item_reminder_email(
                        action_item,
                        milestone,
                        reminder_date,
                    )

    def send_reminder_email(self, milestone):
        user = milestone.project.user
        project = milestone.project
        context = {
            "first_name": user.first_name,
            "name": milestone.name,
            "project_name": project.project_name,
            "due_date": milestone.due_date.strftime('%d-%m-%Y'),
            "reminder_settings": milestone.reminder_setting,
            "frontend_url": frontend_url_project_milestone(project.solution)
        }
        html_body = loader.render_to_string('milestone_due_email.html', context)
        plain_message = plain_message_action_item_reminder(
            first_name=user.first_name,
            name=milestone.name,
            project_name=project.project_name,
            due_date=milestone.due_date.strftime('%d-%m-%Y'),
            reminder_settings=milestone.reminder_setting,
            frontend_url=frontend_url_project_milestone(project.solution)
        )
        email_check = get_user_email_preferences(user.id)
        if email_check:
            try:
                send_mail(
                subject='Reminder: Due Milestone',
                message=plain_message,
                html_message=html_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                )
                self.stdout.write(self.style.SUCCESS(f'Reminder email sent to {user.email}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Failed to send reminder email to {user.email}, Reason: {repr(e)}'))

    def send_action_item_reminder_email(
        self,
        action_item: ActionItem,
        milestone,
        reminder_date,
    ):
        if milestone:
            solution = getattr(milestone.project, 'solution', None)
        else:
            first_assignee = action_item.assignees.first()
            solution = get_user_active_solution(first_assignee.id) if first_assignee else None

        assignees = list(action_item.assignees.all())

        for assignee in assignees:
            context = {
                "name": action_item.name,
                "target_date": action_item.target_date.strftime('%d-%m-%Y'),
                "reminder_settings": action_item.reminder,
                "assignee": assignee.full_name if assignee else '',
            }
            if milestone:
                context["milestone_name"] = milestone.name
                context["project_name"] = milestone.project.project_name
                context["frontend_url"] = frontend_url_project_action_item(solution, action_item.id)  
                template = 'action_item_due_email.html'
                plain_message = plain_message_action_item_reminder_with_milestone(
                    assignee=assignee.full_name if assignee else '',
                    name=action_item.name,
                    milestone_name=milestone.name,
                    project_name=milestone.project.project_name,
                    target_date=action_item.target_date.strftime('%d-%m-%Y'),
                    reminder_settings=action_item.reminder,
                    frontend_url=frontend_url_project_action_item(solution, action_item.id),
                )
            else:
                context["company_name"] = action_item.company_profile.company_name if action_item.company_profile else None
                context["frontend_url"] = frontend_url_company_action_item(solution, action_item.id)
                template = 'company_action_item_due_email.html'
                plain_message = plain_message_action_item_reminder_with_company(
                    assignee=assignee.full_name if assignee else '',
                    name=action_item.name,
                    target_date=action_item.target_date.strftime('%d-%m-%Y'),
                    reminder_settings=action_item.reminder,
                    frontend_url=frontend_url_company_action_item(solution, action_item.id),
                    company_name=action_item.company_profile.company_name if action_item.company_profile else None
                )

            email_check = get_user_email_preferences(assignee.id)
            if email_check:
                delivery = self.claim_action_item_reminder_delivery(
                    action_item,
                    assignee,
                    reminder_date,
                )
                if delivery is None:
                    continue

                try:
                    send_mail(
                        subject=f'Reminder: Action Item Due - {action_item.name}',
                        message=plain_message,
                        html_message=loader.render_to_string(template, context),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[assignee.email] if assignee else [],
                    )
                    delivery.status = ActionItemReminderDelivery.Status.SENT
                    delivery.sent_at = timezone.now()
                    delivery.last_error = ""
                    delivery.save(
                        update_fields=["status", "sent_at", "last_error", "updated_at"]
                    )
                    print(f'Reminder email sent to {assignee.email if assignee else None}')
                except Exception as e:
                    delivery.status = ActionItemReminderDelivery.Status.FAILED
                    delivery.last_error = str(e)[:2000]
                    delivery.save(
                        update_fields=["status", "last_error", "updated_at"]
                    )
                    print(f'Failed to send reminder email to {assignee.email}, Reason: {repr(e)}')

    @staticmethod
    def claim_action_item_reminder_delivery(action_item, assignee, reminder_date):
        with transaction.atomic():
            delivery, created = ActionItemReminderDelivery.objects.get_or_create(
                action_item=action_item,
                assignee=assignee,
                reminder_date=reminder_date,
            )
            if created:
                return delivery

            delivery = ActionItemReminderDelivery.objects.select_for_update().get(
                pk=delivery.pk
            )
            if delivery.status != ActionItemReminderDelivery.Status.FAILED:
                return None

            delivery.status = ActionItemReminderDelivery.Status.PENDING
            delivery.last_error = ""
            delivery.save(update_fields=["status", "last_error", "updated_at"])
            return delivery


    def parse_reminder_settings(self, reminder_settings):
        day_match = re.match(r'(\d+)\s*days?', reminder_settings, re.IGNORECASE)
        week_match = re.match(r'(\d+)\s*weeks?', reminder_settings, re.IGNORECASE)
        month_match = re.match(r'(\d+)\s*months?', reminder_settings, re.IGNORECASE)
        
        if day_match:
            return int(day_match.group(1))
        elif week_match:
            return int(week_match.group(1)) * 7
        elif month_match:
            return int(month_match.group(1)) * 30
        return None
