from django.conf import settings
from django.db import connection
from django.utils.timezone import make_aware, now
from datetime import datetime, timedelta
import boto3
import json
from django.contrib.auth.models import AnonymousUser
from models.models import User
from ecpmpapp.license_queries import get_active_license_sql_condition

def get_frontend_url_for_solution(solution):
    """
    Determines the frontend URL based on the solution purchased.
    Assumes that the solution string is stored on the project or company profile.
    """
    if solution and solution.lower() == 'core solution':
        return settings.FRONTEND_URL_CORE
    elif solution and solution.lower() == 'enterprise solution':
        return settings.FRONTEND_URL_ENTERPRISE
    return settings.FRONTEND_URL_CPM


def get_user_active_solution(user_id):
    """
    Determines the user's active solution based on their licenses.
    Uses raw SQL query to check license status in admin server database.
    
    Args:
        user_id: The ID of the user to check
        
    Returns:
        str: Returns 'Enterprise Solution', 'Energy Consultant Solution', or 'Core Solution'
             based on active licenses. Defaults to 'Enterprise Solution' if no match.
    """
    active_license_condition = get_active_license_sql_condition()
    query = f"""
        SELECT DISTINCT p.name,
            CASE 
                WHEN LOWER(p.name) = 'enterprise solution' THEN 1
                WHEN LOWER(p.name) = 'energy consultant solution' THEN 2
                WHEN LOWER(p.name) = 'core solution' THEN 3
                ELSE 4
            END as priority
        FROM package_license_license AS l
        JOIN package_license_product AS p ON l.product_id = p.id
        JOIN package_license_package AS pkg ON l.package_id = pkg.id
        WHERE l.user_id = %s
            AND {active_license_condition}
        ORDER BY priority;
    """
    
    with connection.cursor() as cursor:
        cursor.execute(query, [user_id])
        result = cursor.fetchone()
        
        if result:
            solution_name = result[0]
            if solution_name.lower() == 'enterprise solution':
                return 'Enterprise Solution'
            elif solution_name.lower() == 'energy consultant solution':
                return 'Energy Consultant Solution'
            elif solution_name.lower() == 'core solution':
                return 'Core Solution'
        
        return 'Enterprise Solution'  # Default fallback


def _calendar_event_owner_email(event):
    """Resolve owner email for reminder payloads (user or created_by)."""
    owner = getattr(event, 'user', None) or getattr(event, 'created_by', None)
    return getattr(owner, 'email', None) if owner else None


def _calendar_event_project_name(event):
    """Return project_name when the calendar event is project-scoped."""
    project = getattr(event, 'project', None)
    if not project:
        return None
    return getattr(project, 'project_name', None) or None


def _calendar_event_email_event_name(event):
    """Event title for reminder emails; appends project name when present."""
    project_name = _calendar_event_project_name(event)
    if project_name:
        return f'{event.name} for project "{project_name}"'
    return event.name


def send_event_to_sqs(event):
    try:
        # Initialize SQS client
        sqs_client = boto3.client('sqs', region_name=settings.AWS_SQS_REGION_NAME)

        # Create the message body
        message_body = {
            'event_id': event.id,
            'event_name': _calendar_event_email_event_name(event),
            'project_name': _calendar_event_project_name(event),
            'start_time': event.start_date.isoformat(),
            'end_time': event.end_date.isoformat(),
            'minutes': 10,
            'user_email': event.user.email,
            'participants': [assignee.email for assignee in event.assignees.all()]
        }

        # Send the message to SQS with a delay
        sqs_client.send_message(
            QueueUrl=settings.AWS_SQS_QUEUE_URL,
            MessageBody=json.dumps(message_body),  # Convert to JSON string
        )
    except Exception as e:
        raise Exception(f"Failed to send event to SQS: {e}")


def _calendar_reminder_message_body(event, label):
    return {
        "event_id": event.id,
        "event_name": _calendar_event_email_event_name(event),
        "project_name": _calendar_event_project_name(event),
        "start_time": event.start_date.isoformat(),
        "end_time": event.end_date.isoformat(),
        "minutes": label,
        "participants": [assignee.email for assignee in event.assignees.all()],
        "user_email": _calendar_event_owner_email(event),
        "meeting_link": event.meeting_link,
    }


def _calendar_reminder_slots(event):
    """Return (label, fire_at) pairs for 30- and 15-minute reminders still in the future."""
    event_time = event.start_date
    reminders = []
    if event_time - timedelta(minutes=30) > now():
        reminders.append(("30", event_time - timedelta(minutes=30)))
    if event_time - timedelta(minutes=15) > now():
        reminders.append(("15", event_time - timedelta(minutes=15)))
    return reminders


def _schedule_inapp_calendar_reminders(event):
    """Schedule Celery tasks that create notification-bell reminders."""
    try:
        from userapis.tasks import send_calendar_event_reminder_notification
    except Exception as e:
        print(f"In-app calendar reminder task unavailable: {e}")
        return

    expected_start = event.start_date.isoformat()
    for label, schedule_time in _calendar_reminder_slots(event):
        try:
            send_calendar_event_reminder_notification.apply_async(
                args=[event.id, str(label), expected_start],
                eta=schedule_time,
            )
        except Exception as e:
            print(
                f"Failed to schedule in-app calendar reminder "
                f"({label}min) for event {event.id}: {e}"
            )


def schedule_reminder_to_sqs(event):
    env = settings.ENV if settings.ENV else "test"
    scheduler = boto3.client('scheduler', region_name=settings.AWS_SQS_REGION_NAME)
    queue_arn = settings.AWS_SQS_QUEUE_ARN
    role_arn = settings.AWS_SQS_ROLE_ARN

    for label, schedule_time in _calendar_reminder_slots(event):
        schedule_name = f"reminder_{env}_{event.id}_{label}min"
        timestamp = schedule_time.strftime('%Y-%m-%dT%H:%M:%S')

        scheduler.create_schedule(
            Name=schedule_name,
            ScheduleExpression=f"at({timestamp})",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": queue_arn,
                "RoleArn": role_arn,
                "Input": json.dumps(_calendar_reminder_message_body(event, label))
            },
            ActionAfterCompletion="DELETE",
        )

    _schedule_inapp_calendar_reminders(event)

def reschedule_event(event, is_deleted=False):
    env = settings.ENV if settings.ENV else "test"
    scheduler = boto3.client('scheduler', region_name=settings.AWS_SQS_REGION_NAME)
    queue_arn = settings.AWS_SQS_QUEUE_ARN
    role_arn = settings.AWS_SQS_ROLE_ARN
    
    schedule_15 = f"reminder_{env}_{event.id}_15min"
    schedule_30 = f"reminder_{env}_{event.id}_30min"
    # Delete old schedules
    try:
        scheduler.delete_schedule(Name=schedule_15)
    except Exception as e:
        print(f"15-min schedule not found or already deleted: {e}")

    try:
        scheduler.delete_schedule(Name=schedule_30)
    except Exception as e:
        print(f"30-min schedule not found or already deleted: {e}")

    if is_deleted:
        return

    for label, schedule_time in _calendar_reminder_slots(event):
        schedule_name = f"reminder_{env}_{event.id}_{label}min"
        timestamp = schedule_time.strftime('%Y-%m-%dT%H:%M:%S')

        # Recreate schedules
        scheduler.create_schedule(
            Name=schedule_name,
            ScheduleExpression=f"at({timestamp})",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": queue_arn,
                "RoleArn": role_arn,
                "Input": json.dumps(_calendar_reminder_message_body(event, label)),
            },
            ActionAfterCompletion="DELETE",
        )

    _schedule_inapp_calendar_reminders(event)


def can_assign_calendar_event(user, assignee):
    """
    Checks if a user can assign a calendar event to an assignee based on company relationships,
    project teams, and connections.
    
    Args:
        user: The user creating/updating the calendar event
        assignee: The user being assigned to the calendar event
        
    Returns:
        bool: True if the assignment is allowed, False otherwise
    """
    from project_management.utils import has_accepted_connection, has_accepted_internal_user_connection
    
    # If user is a freelancer
    if user.is_freelancer:
        # Allow self-assignment
        if assignee == user:
            return True
            
        # Allow project team members
        if user.project_teams.filter(project__team_members__team_member=assignee).exists():
            return True
            
        # Allow connected freelancers
        if assignee.is_freelancer and has_accepted_connection(
            user.sharemundo_links_id, 
            assignee.sharemundo_links_id
        ):
            return True
            
        return False
        
    # If user is from company
    else:
        # Allow same company users
        if assignee.company and assignee.company == user.company:
            return True
            
        # Allow project team members
        if user.project_teams.filter(project__team_members__team_member=assignee).exists():
            return True
            
        # Allow connected freelancers
        if assignee.is_freelancer and has_accepted_connection(
            user.company.sharemundo_links_id,
            assignee.sharemundo_links_id
        ):
            return True
            
        # Allow connected company users
        if (assignee.company and has_accepted_internal_user_connection(
            user.company.sharemundo_links_id,
            assignee.company.sharemundo_links_id,
            assignee.id
        )):
            return True
            
        return False

def get_user_role(user, type, scope, context=None):
    """
    Get user's role for a specific scope.
    Returns either a UserRole object (for company scope) or a Role object (for project scope).
    """
    from models.models import UserRole
    from project_management.models import ProjectTeamMembers
    
    # Handle freelancer permissions
    if user.is_freelancer:
        if scope == 'company':
            # For company scope, check freelancer's solution role (no company)
            return UserRole.objects.filter(
                user=user,
                role__scope='company',
                role__type='solution',
                role__company__isnull=True  # Freelancers don't have a company
            ).first()
        else:
            # For project scope, check project role (same as company users)
            project = context.get('project', None) if context else None
            if not project:
                return None
            member = ProjectTeamMembers.objects.filter(
                project=project,
                team_member=user
            ).first()
            return member.project_role if member else None
    
    # Handle company user permissions
    if scope == 'company':
        # Company level: type=solution, scope=company
        return UserRole.objects.filter(
            user=user,
            role__scope='company',
            role__type='solution',
            role__company=user.company
        ).first()
    else:
        # Project level: type=solution, scope=project
        project = context.get('project', None) if context else None
        if not project:
            return None
        member = ProjectTeamMembers.objects.filter(
            project=project,
            team_member=user
        ).first()
        return member.project_role if member else None


def is_company_admin(user):
    """
    Check if a user is a company administrator.
    A user is considered a company admin if they have the SOLUTION_COMPANY_ADMINISTRATOR role at the company level.
    
    Args:
        user: The user to check
        
    Returns:
        bool: True if the user is a company admin, False otherwise
    """
    if not user or not user.company:
        return False
        
    from models.models import UserRole
    from models.constants import SOLUTION_COMPANY_ADMINISTRATOR
    
    return UserRole.objects.filter(
        user=user,
        role__name=SOLUTION_COMPANY_ADMINISTRATOR,
        role__scope='company',
        role__type='solution',
        role__company=user.company
    ).exists()


def is_user_company_owner(user):
    """
    Check if a user is the company owner of their own company.
    A user is considered a company owner if they are the admin_user of the company.
    
    Args:
        user: The user to check
        
    Returns:
        bool: True if the user is the company owner, False otherwise
    """
    if not user or not user.company:
        return False
        
    return user.company.admin_user == user


def require_permission(user, module, action, scope, kwargs):
    user_role = get_user_role(user, module, scope, kwargs)
    if not user_role:
        return False
    permission_key = f"{module}.{action}"
    
    # Handle both UserRole and Role objects
    if hasattr(user_role, 'role'):
        # UserRole object - access through role relationship
        return user_role.role.role_permissions.filter(
            permission__key=permission_key
        ).exists()
    else:
        # Role object - access directly
        return user_role.role_permissions.filter(
            permission__key=permission_key
        ).exists()

def require_any_permission(user, module, actions, scope, kwargs):
    user_role = get_user_role(user, module, scope, kwargs)
    if not user_role:
        return False
    permission_keys = [f"{module}.{action}" for action in actions]
    print(permission_keys)
    
    # Handle both UserRole and Role objects
    if hasattr(user_role, 'role'):
        # UserRole object - access through role relationship
        return user_role.role.role_permissions.filter(
            permission__key__in=permission_keys
        ).exists()
    else:
        # Role object - access directly
        return user_role.role_permissions.filter(
            permission__key__in=permission_keys
        ).exists()

def require_all_permissions(user, module, actions, scope, kwargs):
    for action in actions:
        if not require_permission(user, module, action, scope, kwargs):
            return False
    return True

def require_any_permission_multiple_modules(user, modules_actions, scope, kwargs):
    for module, actions in modules_actions.items():
        if require_any_permission(user, module, actions, scope, kwargs):
            return True
    return False



def require_all_permissions_multiple_modules(user, modules_actions, scope, kwargs):
    for module, actions in modules_actions.items():
        if not require_all_permissions(user, module, actions, scope, kwargs):
            return False
    return True
# Notification utility functions
def should_notify_user(user, notification_type):
    """
    Check if a user should be notified based on their preferences.
    
    Args:
        user: User instance
        notification_type: String representing the type of notification (e.g., "Project", "Opportunity")
    
    Returns:
        bool: True if user should be notified, False otherwise
    """
    try:
        from project_management.utils import get_user_notification_preferences
        prefs = get_user_notification_preferences(user.id)
        return (
            prefs["notify_created"] or 
            prefs["notify_own"] or 
            prefs["notify_part"] or 
            (notification_type in prefs["own_types"]) or 
            (notification_type in prefs["part_types"]) or
            (notification_type in prefs.get("company_own_types", [])) or
            (notification_type in prefs.get("company_part_types", []))
        )
    except Exception:
        return False

def create_notification_with_recipients(message, content_type, object_id, content_object, notification_type, recipients, important=False, action_type='create', project=None, bypass_preference_check=False):
    """
    Create a notification and send it to recipients based on their preferences.
    
    Args:
        message: Notification message
        content_type: ContentType instance
        object_id: ID of the related object
        notification_type: Type of notification (create, update, delete)
        recipients: Set of User instances
        important: Whether the notification is important
    
    Returns:
        Notification instance or None if no recipients should be notified
    """
    try:
        from models.models import Notification, NotificationSeen
        
        # Check if any user should be notified
        should_notify = bool(recipients) if bypass_preference_check else any(
            should_notify_user_comprehensive(
                user,
                notification_type,
                content_object,
                action_type=action_type,
                project=project,
            )
            for user in recipients
        )
        if not should_notify:
            return None
            
        # Create the notification
        notification = Notification.objects.create(
            message=message,
            content_type=content_type,
            object_id=object_id,
            type=action_type,
            important=important,
            backend_type='backend',
        )
        
        # Create NotificationSeen records for users who should be notified
        for user in recipients:
            if bypass_preference_check or should_notify_user_comprehensive(
                user,
                notification_type,
                content_object,
                action_type=action_type,
                project=project,
            ):
                NotificationSeen.objects.create(user=user, notification=notification)
                notification.user.add(user)

        
        return notification
    except Exception as e:
        print(f"Error creating notification: {e}")
        return None

def format_notification_message(action_type, object_name, actor_name, field_changes=None, project_name=None):
    """
    Format notification messages consistently.
    
    Args:
        action_type: create, update, or delete
        object_name: Name of the object being acted upon
        actor_name: Name of the person performing the action
        field_changes: List of field names that changed (optional)
        project_name: Name of the project (optional)
    
    Returns:
        str: Formatted notification message
    """
    
    if action_type == 'create':
        return f'{object_name} was created by {actor_name}.'
    elif action_type == 'update':
        if field_changes:
            field_list = ", ".join(field_changes)
            return f'{object_name} was updated by {actor_name} (fields: {field_list}).'
        else:
            return f'{object_name} was updated by {actor_name}'
    elif action_type == 'delete':
        return f'{object_name} was deleted by {actor_name}.'
    
    return f'{actor_name} performed {action_type} on {object_name}.'

def get_actor_name(requester):
    """Return a consistent actor name for notifications."""
    if not requester or isinstance(requester, AnonymousUser):
        return "System"
    
    full_name = getattr(requester, 'full_name', None)
    if full_name:
        return full_name

    return "User"




def resolve_company_profile(content_object):
    """Resolve the company profile related to a content object, if any."""
    try:
        model_name = content_object.__class__.__name__

        if model_name == 'Document':
            folder = getattr(content_object, 'folder', None)
            return getattr(folder, 'company_profile', None) if folder else None

        if model_name == 'LeaveRequest':
            employee = getattr(content_object, 'employee', None)
            employee_user = getattr(employee, 'user', None) if employee else None
            return getattr(employee_user, 'company', None) if employee_user else None

        if model_name == 'MonthlyTimesheetSubmission':
            submission_user = getattr(content_object, 'user', None)
            return getattr(submission_user, 'company', None) if submission_user else None

        if getattr(content_object, 'company_profile', None):
            return content_object.company_profile

        folder = getattr(content_object, 'folder', None)
        if folder is not None:
            return getattr(folder, 'company_profile', None)

        return None
    except Exception:
        return None


def get_instance_notification_scope(content_object, project=None):
    """
    Determine whether a notification object is project-scoped or company-scoped.
    Does not read user preference arrays.
    """
    try:
        if project is not None:
            return "project"

        if getattr(content_object, "project_id", None) or getattr(content_object, "project", None):
            return "project"

        milestone = getattr(content_object, "milestone", None)
        if milestone is not None and (
            getattr(milestone, "project_id", None) or getattr(milestone, "project", None)
        ):
            return "project"

        folder = getattr(content_object, "folder", None)
        if folder is not None:
            if getattr(folder, "project_id", None) or getattr(folder, "project", None):
                return "project"
            if getattr(folder, "company_profile_id", None) or getattr(folder, "company_profile", None):
                return "company"

        model_name = content_object.__class__.__name__
        if model_name in {"LeaveRequest", "MonthlyTimesheetSubmission"}:
            return "company"

        if model_name == "CalendarEvent":
            if getattr(content_object, "project_id", None) or getattr(content_object, "project", None):
                return "project"
            return "company"

        if getattr(content_object, "company_profile_id", None) or getattr(content_object, "company_profile", None):
            return "company"

        if resolve_company_profile(content_object) is not None:
            return "company"

        return None
    except Exception as e:
        print(f"Error resolving notification scope for {content_object.__class__.__name__}: {e}")
        return None


def should_notify_user_comprehensive(user, notification_type, content_object, action_type='create', project=None):
    """
    Comprehensive notification check that handles all the requirements.
    
    Args:
        user: User instance
        notification_type: String representing the type of notification
        content_object: The object being notified about
        action_type: create, update, or delete
        project: Project instance
    Returns:
        bool: True if user should be notified, False otherwise
    """
    try:
        from project_management.utils import get_user_notification_preferences
        from ecpmpapp.utils import get_user_active_solution
        
        prefs = get_user_notification_preferences(user.id)
        user_solution = get_user_active_solution(user.id)
        scope = get_instance_notification_scope(content_object, project)
        
        # 1. Default Rule: "All items assigned to me" (cannot be changed)
        if is_user_assigned_to_item(user, content_object):
            return True
        
        # 2. "All items created by me"
        if prefs["notify_created"] and is_user_creator_of_item(user, content_object):
            return True

        # Project-scoped specific preferences
        if scope == "project":
            if (user_solution in ['Enterprise Solution', 'Energy Consultant Solution'] and 
                notification_type in prefs["own_types"] and 
                is_user_project_owner(user, content_object, project)):
                return True

            if (notification_type in prefs["part_types"] and 
                is_user_project_member(user, content_object, project)):
                return True

        # Company-scoped specific preferences
        if scope == "company":
            if (
                notification_type in prefs["company_own_types"]
                and is_company_owner(user, content_object)
            ):
                return True

            if (
                notification_type in prefs["company_part_types"]
                and is_user_company_member(user, content_object)
            ):
                return True
        
        return False
        
    except Exception as e:
        print(f"Error in should_notify_user_comprehensive: {e}")
        return False

def is_company_owner(user, content_object):
    """
    Check if user is the owner of the company related to the item.
    """
    company_profile = resolve_company_profile(content_object)
    return bool(
        company_profile
        and getattr(company_profile, 'admin_user', None) == user
    )


def is_user_company_member(user, content_object):
    """Check if user belongs to the company related to the item."""
    company_profile = resolve_company_profile(content_object)
    return bool(company_profile and getattr(user, 'company_id', None) == company_profile.id) 

def is_user_assigned_to_item(user, content_object):
    """
    Check if user is assigned to the item based on object type.
    
    Args:
        user: User instance
        content_object: The object to check assignment for
    
    Returns:
        bool: True if user is assigned to the item
    """
    try:
        # Get the model name to determine assignment logic
        model_name = content_object.__class__.__name__
        
        if model_name == 'ActionItem':
            if hasattr(content_object, 'assignees') and content_object.assignees.filter(id=user.id).exists():
                return True
            return hasattr(content_object, 'assignee') and content_object.assignee == user
            
        elif model_name == 'CalendarEvent':
            # CalendarEvent has multiple assignees
            return hasattr(content_object, 'assignees') and user in content_object.assignees.all()
            
        elif model_name == 'TimeCard':
            # TimeCard is assigned to the company admin
            company_profile = content_object.company_profile
            if company_profile:
                return hasattr(company_profile, 'admin_user') and company_profile.admin_user == user
            
        elif model_name == 'ExpenseCard':
            # ExpenseCard is assigned to the company admin
            company_profile = content_object.company_profile
            if company_profile:
                return hasattr(company_profile, 'admin_user') and company_profile.admin_user == user
            
        elif model_name == 'LeaveRequest':
            employee = getattr(content_object, 'employee', None)
            employee_user = getattr(employee, 'user', None) if employee else None
            if employee_user == user:
                return True
            return (
                hasattr(content_object, 'employee')
                and hasattr(content_object.employee, 'supervisor')
                and content_object.employee.supervisor
                and content_object.employee.supervisor.user == user
            )

        elif model_name == 'MonthlyTimesheetSubmission':
            return getattr(content_object, 'user', None) == user
            
        elif model_name == 'Document':
            # Document is assigned to the author
            return hasattr(content_object, 'author') and content_object.author == user
                
        elif model_name == 'Invoice':
            # Invoice is assigned to the company admin
            company_profile = content_object.company_profile
            if company_profile:
                return hasattr(company_profile, 'admin_user') and company_profile.admin_user == user
                
        elif model_name == 'Project':
            # Project owner is considered "assigned"
            return hasattr(content_object, 'user') and content_object.user == user
            
        elif model_name == 'Milestone':
            # Milestone is assigned to the project owner
            return hasattr(content_object, 'project') and hasattr(content_object.project, 'user') and content_object.project.user == user
                
        elif model_name == 'ProjectTeamMembers':
            # Team member is assigned to the project
            return hasattr(content_object, 'team_member') and content_object.team_member == user
            
        elif model_name == 'Opportunity':
            # Opportunity is assigned to freelancer
            return hasattr(content_object, 'freelancer') and content_object.freelancer == user
        
        elif model_name == 'Feedback':
            # Feedback is assigned to the user who created it
            return hasattr(content_object, 'user') and content_object.user == user
        
        elif model_name == 'ProjectPhase':
            # ProjectPhase is assigned to the project owner
            return hasattr(content_object, 'project') and hasattr(content_object.project, 'user') and content_object.project.user == user
        
        return False
    except Exception as e:
        print(f"Error checking assignment for {content_object.__class__.__name__}: {e}")
        return False

def is_user_creator_of_item(user, content_object):
    """
    Check if user created the item.
    
    Args:
        user: User instance
        content_object: The object to check creation for
    
    Returns:
        bool: True if user created the item
    """
    try:
        model_name = content_object.__class__.__name__
        
        if model_name == 'Document':
            # Document is created by the author
            return hasattr(content_object, 'author') and  content_object.author == user
                
        elif model_name == 'CalendarEvent':
            # CalendarEvent is created by the creator
            return hasattr(content_object, 'user') and content_object.user == user
        elif model_name == 'Project':
            # Project is created by the user
            return hasattr(content_object, 'user') and content_object.user == user
        elif model_name == 'Milestone':
            # Milestone is created by the project owner
            return hasattr(content_object, 'project') and hasattr(content_object.project, 'user') and content_object.project.user == user
        elif model_name == 'ProjectTeamMembers':
            # ProjectTeamMembers is created by the project owner
            return hasattr(content_object, 'project') and hasattr(content_object.project, 'user') and content_object.project.user == user
        elif model_name == 'Opportunity':
            # Opportunity is created by the freelancer
            return (hasattr(content_object, 'freelancer') and content_object.freelancer == user) or (hasattr(content_object, 'created_by') and content_object.created_by == user)
        elif model_name == 'TimeCard':
            # TimeCard is created by the company admin
            return hasattr(content_object, 'created_by') and content_object.created_by == user
        elif model_name == 'ActionItem':
            # ActionItem is created by the user
            return hasattr(content_object, 'created_by') and content_object.created_by == user
        elif model_name == 'ExpenseCard':
            # ExpenseCard is created by the company admin
            return hasattr(content_object, 'created_by') and content_object.created_by == user
        elif model_name == 'LeaveRequest':
            # LeaveRequest is created by the employee
            return hasattr(content_object, 'employee') and hasattr(content_object.employee, 'user') and content_object.employee.user == user
        elif model_name == 'ContactUser':
            # ContactUser is created by the company admin
            return hasattr(content_object, 'company_profile') and hasattr(content_object.company_profile, 'admin_user') and content_object.company_profile.admin_user == user
        elif model_name == 'ContactCompany':
            # ContactCompany is created by the company admin
            return hasattr(content_object, 'company_profile') and hasattr(content_object.company_profile, 'admin_user') and content_object.company_profile.admin_user == user
        elif model_name == 'ProjectPhase':
            # ProjectPhase is created by the project owner
            return (hasattr(content_object, 'created_by') and content_object.created_by == user)
        elif model_name == 'Feedback':
            # Feedback is created by the user
            return hasattr(content_object, 'user') and content_object.user == user
        
        return False
        
    except Exception as e:
        print(f"Error checking creator for {content_object.__class__.__name__}: {e}")
        return False

def is_user_project_owner(user, content_object, project=None):
    """
    Check if user owns the project related to the item.
    
    Args:
        user: User instance
        content_object: The object to check project ownership for
    
    Returns:
        bool: True if user owns the project
    """
    try:
        if not project:
            return False
        return hasattr(project, 'user') and project.user == user
        
    except Exception as e:
        print(f"Error checking project ownership: {e}")
        return False

def is_user_project_member(user, content_object, project=None):
    """
    Check if user is a member of the project related to the item.
    
    Args:
        user: User instance
        content_object: The object to check project membership for
    
    Returns:
        bool: True if user is a project member
    """
    try:
        if not project:
            return False
        return hasattr(project, 'team_members') and project.team_members.filter(team_member=user).exists()
        
    except Exception as e:
        print(f"Error checking project membership: {e}")
        return False

def get_validate_recipients(recipients, requester=None):
    """
    Validate and filter recipients to ensure they are valid users in the system.
    
    Args:
        recipients (list): List of recipient email addresses or user IDs.
        
    Returns:
        list: Validated list of User objects.
    """
    recipients = [recipient for recipient in recipients if recipient]
    recipient_ids = {user.id for user in recipients}

    if requester and requester.id in recipient_ids:
        recipient_ids.discard(requester.id)
    validated_qs = User.objects.filter(id__in=recipient_ids)
    return set(validated_qs)

def check_for_freelancer_or_company_owner(user, content_object):
    """
    Check if user is a freelancer or company owner.
    """
    return (user.is_freelancer and user == content_object.user) or is_company_owner(user, content_object) or (user.groups.filter(name='admin').exists() and user.company == content_object.company_profile)
