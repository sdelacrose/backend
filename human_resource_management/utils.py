from django.core.mail import send_mail, EmailMultiAlternatives
from django.conf import settings
from datetime import timedelta
import numpy as np
import datetime
from django.template import loader
from models.models import User

from ecpmpapp.alt_messages import (
    plain_messages_for_send_request_notification_email,
    plain_messages_for_send_request_status_email,
    plain_message_leave_balance_reminder,
)
from human_resource_management.constants import full_approval_link
from ecpmpapp.constants import LeaveType
from userapis.utils import get_user_email_preferences


def get_leave_reviewers(employee_user):
    """Return active same-company users with leave review permission."""
    if employee_user.is_freelancer or not employee_user.company_id:
        return User.objects.none()

    return User.objects.filter(
        company_id=employee_user.company_id,
        is_active=True,
        is_deleted=False,
        user_company_roles__role__company_id=employee_user.company_id,
        user_company_roles__role__scope='company',
        user_company_roles__role__type='solution',
        user_company_roles__role__is_active=True,
        user_company_roles__role__role_permissions__permission__key=(
            'leave_request.review'
        ),
    ).exclude(id=employee_user.id).distinct()

def is_weekend(d: datetime.date):
    return d.weekday() >= 5

def calculate_leave_days(start_date, end_date):
    """
    Calculate the number of leave days between two dates, excluding weekends.
    
    :param start_date: datetime.date - Start date of leave
    :param end_date: datetime.date - End date of leave
    :return: int - Number of business days
    """
    if not start_date or not end_date:
        return 0
    
    return np.busday_count(start_date, end_date + timedelta(days=1))

def send_request_notification_email(employee, reviewer, request):
    """
    Send an email notification to one eligible leave reviewer.
    """
    context = {
        "manager_name": reviewer.first_name,
        "employee_name": employee.full_name,
        "request_type": LeaveType.WORK_FROM_HOME.value if request.is_working_from_home else LeaveType.LEAVE.value,
        "leave_type": request.type.capitalize() if not request.is_working_from_home else None,
        "start_date": request.start_date.strftime('%d-%m-%Y'),
        "end_date": request.end_date.strftime('%d-%m-%Y'),
        "reason": request.reason if request.reason else "N/A",
        "approval_link": full_approval_link(reviewer),
    }

    html_body = loader.render_to_string('request_notification.html', context)
    plain_messages = plain_messages_for_send_request_notification_email(
        manager_name=reviewer.first_name,
        employee_name=employee.full_name,
        request_type=LeaveType.WORK_FROM_HOME.value if request.is_working_from_home else LeaveType.LEAVE.value,
        leave_type=request.type.capitalize() if not request.is_working_from_home else None,
        start_date=request.start_date.strftime('%d-%m-%Y'),
        end_date=request.end_date.strftime('%d-%m-%Y'),
        reason=request.reason if request.reason else "N/A",
        approval_link=full_approval_link(reviewer)
    )
    email_check = get_user_email_preferences(reviewer.id)
    if email_check:
        try:
            send_mail(
                subject="New Leave Request Notification",
                message=plain_messages,
                html_message=html_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[reviewer.email],
                fail_silently=False
            )
        except Exception as e:
            raise Exception(f'Failed to send leave request notification email to {reviewer.email}, Reason: {repr(e)}')

def send_request_status_email(employee, reviewer, request, status):
    """
    Sends an email notification to the employee about their leave or work from home request approval/rejection.
    """

    context = {
        "employee_name": employee.first_name,
        "manager_name": "Unknown" if reviewer is None else reviewer.full_name,
        "request_type": LeaveType.WORK_FROM_HOME.value if request.is_working_from_home else LeaveType.LEAVE.value,
        "leave_type": request.type.capitalize() if not request.is_working_from_home else None,
        "start_date": request.start_date.strftime('%d-%m-%Y'),
        "end_date": request.end_date.strftime('%d-%m-%Y'),
        "reason": request.reason if request.reason else "N/A",
        "status": "Approved" if status == "approved" else "Rejected",
    }

    html_body = loader.render_to_string('request_status_update.html', context)
    plain_messages = plain_messages_for_send_request_status_email(
        employee_name=employee.first_name,
        manager_name="Unknown" if reviewer is None else reviewer.full_name,
        request_type=LeaveType.WORK_FROM_HOME.value if request.is_working_from_home else LeaveType.LEAVE.value,
        leave_type=request.type.capitalize() if not request.is_working_from_home else None,
        start_date=request.start_date.strftime('%d-%m-%Y'),
        end_date=request.end_date.strftime('%d-%m-%Y'),
        reason=request.reason if request.reason else "N/A",
        status="Approved" if status == "approved" else "Rejected",
    )
    email_check = get_user_email_preferences(employee.id)
    if email_check:
        try:
            send_mail(
                subject=f"Your {context['request_type']} Request {context['status']}",
                message=plain_messages,
                html_message=html_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[employee.email],
                fail_silently=False
            )
        except Exception as e:
            raise Exception(f'Failed to send leave request status email to {employee.email}, Reason: {repr(e)}')


def send_leave_balance_reminder_email(staff, deadline_date, balances, *, to_user):
    """AI-1685 follow-up: one independent email per RECIPIENT (`to_user` --
    the subject's own user, or a single CC'd HR user) -- never a single
    multi-recipient to/cc send, so a transport failure or retry for one
    recipient can never affect another's own already-delivered message
    (see services.leave_reminders._deliver_to_recipient, which calls this
    once per recipient with its own independent claim/retry). Same gated-
    send pattern as send_request_notification_email/send_request_status_
    email above (get_user_email_preferences check is the CALLER's
    responsibility -- services.leave_reminders already checks it per
    recipient before calling this). `balances` never includes salary/
    hourly_rate -- those fields are never read by services.leave_reminders
    in the first place."""
    context = {
        "employee_name": staff.user.first_name,
        "deadline_date": deadline_date.strftime('%d-%m-%Y'),
        "balances": balances,
    }
    html_body = loader.render_to_string('leave_balance_reminder.html', context)
    plain_message = plain_message_leave_balance_reminder(
        employee_name=staff.user.first_name,
        deadline_date=deadline_date.strftime('%d-%m-%Y'),
        balances=balances,
    )
    try:
        message = EmailMultiAlternatives(
            subject="Reminder: review your leave balance",
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_user.email],
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
    except Exception as e:
        raise Exception(f'Failed to send leave balance reminder email to {to_user.email}, Reason: {repr(e)}')
