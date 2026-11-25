import logging

from django.db.models.signals import post_save, pre_delete, m2m_changed
from django.dispatch import receiver
from django.db import transaction
from django.db.models import Q
from django.utils.timezone import timedelta
from collections import defaultdict
from ecpmpapp.middleware import get_current_user
from django.utils.text import capfirst
from django.core.exceptions import FieldDoesNotExist
from django.contrib.contenttypes.models import ContentType

from models.models import User, Role, UserRole
from project_management.models import (
    Project, Folder,
    ProjectTeamMembers, ProjectExternalUsers, 
    Milestone, ActionItem, ProjectFunding, 
    ProjectPhase, ObjectAssessment, PurchaseOrder, 
    Document, Invoice, TimeCard, ExpenseCard,
    ActionItemComment, AvailableProductPart, BillOfMaterialPart,
    CompanySolutionSpaceItem, Equipment, Machine, MatrixComment,
    ProjectSolutionSpaceItem, Requirement, SolutionDemand, Transport,
    SearchRecord, ProjectMatrixConfig, PartsListConfig, SolutionSpaceConfig,
    MachineConfig, EquipmentConfig, PurchaseOrderLineItem, InvoiceLineItem,
)
from project_management.models.contact import ContactUser, ContactCompany
from project_management.models import constants
from chat.models import ChatRoom, MessageRead
from ecpmpapp.constants import UserType

from django.db.models.signals import post_save
from django.dispatch import receiver
from models.models import Notification, NotificationSeen
from ecpmpapp.constants import (
    CUSTOM_LABELS_EXPENSECARD,
    CUSTOM_LABELS_TIMECARD,
    CUSTOM_LABELS_ACTION_ITEMS,
    CUSTOM_LABELS_PROJECTS,
    CUSTOM_TEAM_MEMBER_LABELS,
    CUSTOM_LABELS_MILESTONE, 
    CUSTOM_LABELS_PROJECT_FUNDING,
    CUSTOM_LABELS_OBJECT_ASSESSMENT,
    CUSTOM_LABELS_PURCHASE_ORDER,
    CUSTOM_LABELS_PROJECT_PHASE,
    CUSTOM_LABELS_DOCUMENT,
    CUSTOM_LABELS_FOLDER,
    CUSTOM_LABELS_INVOICE,
    CUSTOM_LABELS_CONTACT_USER,
    CUSTOM_LABELS_CONTACT_COMPANY
)

from ecpmpapp.utils import (
    create_notification_with_recipients, 
    format_notification_message, get_actor_name,
    get_validate_recipients,
)
from project_management.services.project_configuration_templates import (
    create_project_configuration,
)


logger = logging.getLogger(__name__)


def _enqueue_action_item_notification(task_name, *args):
    try:
        from project_management import tasks

        getattr(tasks, task_name).delay(*args)
    except Exception:
        logger.exception(
            "Unable to enqueue Action Item notification task %s.",
            task_name,
        )


def label_for_action_item(field_name):
    """Return a human‑friendly label for a given ActionItem field."""
    if field_name in CUSTOM_LABELS_ACTION_ITEMS:
        return CUSTOM_LABELS_ACTION_ITEMS[field_name]
    try:
        field = ActionItem._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

def label_for(field_name):
    if field_name in CUSTOM_LABELS_PROJECTS:
        return CUSTOM_LABELS_PROJECTS[field_name]
    try:
        field = Project._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()
    
def get_project_recipients(project):
    """
    Get all recipients for project notifications.
    
    Includes:
    - Project owner (project.user)
    - Company admins (all users with ADMIN group in the company)
    - All team members of the project
    
    Args:
        project: Project instance
    
    Returns:
        set: Set of User instances to notify
    """
    recipients = set()

    if not project:
        return recipients
    
    # 1. Add project owner (project.user)
    if project.user:
        recipients.add(project.user)
    
    # 2. Add project creator if different from owner
    if project.created_by and project.created_by != project.user:
        recipients.add(project.created_by)
    
    # 3. Add company admins (all users with ADMIN group in the company)
    if project.company_profile:
        # Get all users in the company who have ADMIN group
        company_admins = User.objects.filter(
            company=project.company_profile,
            groups__name=UserType.ADMIN.value,
            is_active=True
        ).distinct()
        recipients.update(company_admins)
    else:
        # If no company_profile, check if project.user is an admin
        if project.user and not project.user.is_freelancer:
            if project.user.groups.filter(name=UserType.ADMIN.value).exists():
                recipients.add(project.user)
    
    # 4. Add all team members of the project
    team_memberships = ProjectTeamMembers.objects.filter(project=project)
    team_users = User.objects.filter(
        id__in=team_memberships.values_list('team_member_id', flat=True),
        is_active=True
    )
    recipients.update(team_users)
    
    return recipients

@receiver(post_save, sender=Project)
def notify_on_project_save(sender, instance, created, **kwargs):
    recipients = get_project_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)

    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create', 
            f'Project "{instance.project_name}"', 
            actor_name
        )
    else:
        notification_type = 'update'
        changed_fields = instance.tracker.changed()
        meaningful_fields = [field for field in changed_fields if field != 'updated_at']
        if not meaningful_fields:
            return
        field_list = ", ".join(label_for(name) for name in meaningful_fields)
        message = format_notification_message(
            'update',
            f'Project "{instance.project_name}"',
            actor_name,
            field_changes=[field_list]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Project),
        object_id=instance.pk,
        content_object=instance,
        action_type=notification_type,
        notification_type='project',
        recipients=validated_recipients,
        project=instance
    )

@receiver(pre_delete, sender=Project)
def notify_on_project_delete(sender, instance, **kwargs):
    recipients = get_project_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)

    message = format_notification_message(
        'delete',
        f'Project "{instance.project_name}"',
        actor_name
    )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Project),
        object_id=instance.pk,
        content_object=instance,
        notification_type='project',
        action_type='delete',
        recipients=validated_recipients,
        project=instance
    )
    
def adjust_milestone_dates(parent_start, parent_end, child_start, child_end):
    """
    Adjust the child's start and end dates based on the parent's start and end dates.
    
    Scenarios:
    1. If child start is less than parent start and child end is within parent range, adjust only child start.
    2. If both child start and end are less than parent start, shift child start to parent start and adjust child end 
       based on the difference between the original start and end.
    """
    child_modified = False
    duration = (child_end - child_start).days if child_start and child_end else 0

    # Scenario 1: Only adjust the start if it is earlier than the parent start
    if child_start and child_start < parent_start:
        child_start = parent_start
        child_modified = True

    # Scenario 2: If both start and end are before parent start
    if child_start and parent_start and child_end and child_start < parent_start and child_end <= parent_start:
        child_start = parent_start
        child_end = parent_start + timedelta(days=duration)
        child_modified = True


    return child_start, child_end, child_modified

def adjust_action_item_dates(parent_start, parent_end, child_start, child_end):
    """
    Adjust the child's start and end dates based on the parent's start and end dates.
    
    Scenarios:
    1. If child start is less than parent start and child end is within parent range, adjust only child start.
    2. If both child start and end are less than parent start, shift child start to parent start and adjust child end 
       based on the difference between the original start and end.
    """

    child_modified = False
    duration = (child_end - child_start).days if child_start and child_end else 0
    parent_duration = (parent_end - parent_start).days if parent_start and parent_end else 0

    # Scenario 1: Only adjust the start if it is earlier than the parent start
    if child_start and parent_start and child_start < parent_start:
        child_start = parent_start
        child_modified = True

    # Scenario 2: If both start and end are before parent start
    if child_start and parent_start and child_end and child_start < parent_start and child_end <= parent_start:
        child_start = parent_start
        child_end = parent_start + timedelta(days=min(duration, parent_duration))
        child_modified = True
    
    if child_start and parent_end and child_start > parent_end:
        child_end = parent_end
        child_start = parent_end - timedelta(days=min(duration, parent_duration))
        child_modified = True

    if child_start and parent_start and child_end and parent_end and child_start >= parent_start and child_end > parent_end:
        child_end = parent_end
        child_modified = True

    return child_start, child_end, child_modified

@receiver(pre_delete, sender=ActionItem)
def action_item_pre_delete_operations(sender, instance, **kwargs):
    recipients = get_action_item_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)

    message = format_notification_message(
        'delete',
        f'Action Item "{instance.name}"',
        actor_name,
        project_name=instance.milestone.project.project_name if instance.milestone and instance.milestone.project else None
    )
    assignee_recipients = get_action_item_validated_assignee_recipients(
        instance,
        requester,
    )
    if assignee_recipients:
        assignee_message = [
            f'Action Item "{instance.name}" was deleted by {actor_name}.',
        ]
        create_notification_with_recipients(
            message=assignee_message,
            content_type=ContentType.objects.get_for_model(ActionItem),
            object_id=instance.pk,
            content_object=instance,
            notification_type='action_item',
            action_type='delete',
            recipients=assignee_recipients,
            project=instance.milestone.project if instance.milestone and instance.milestone.project else None
        )

    remove_action_item_assignees_from_recipients(validated_recipients, instance)

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ActionItem),
        object_id=instance.pk,
        content_object=instance,
        notification_type='action_item',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.milestone.project if instance.milestone and instance.milestone.project else None
    )

def get_action_item_recipients(action_item):
    user = action_item.created_by
    recipients = set()
    if user:
        recipients = { user }

    if action_item.company_profile and action_item.company_profile.admin_user:
        recipients.add(action_item.company_profile.admin_user)

    if action_item.milestone and action_item.milestone.project:
        project_recipients = get_project_recipients(action_item.milestone.project)
        recipients.update(project_recipients)

    return recipients


def get_action_item_assignee_recipients(action_item):
    return set(action_item.assignees.all())


def get_action_item_validated_assignee_recipients(action_item, requester):
    return get_validate_recipients(
        get_action_item_assignee_recipients(action_item),
        requester,
    )


def remove_action_item_assignees_from_recipients(recipients, action_item):
    recipients.difference_update(get_action_item_assignee_recipients(action_item))
    return recipients

@receiver(post_save, sender=ActionItem)
def action_item_post_save_operations(sender, instance, created, **kwargs):
    if not getattr(instance, "_suppress_action_item_notifications", False):
        requester = get_current_user()
        meaningful = []
        if not created:
            meaningful = [
                field
                for field in instance.tracker.changed()
                if field != "updated_at"
            ]

        if created or meaningful:
            actor_id = getattr(requester, "id", None)
            notify_assignees = not bool(
                getattr(instance, "_assignee_notification_event", None)
            )
            transaction.on_commit(
                lambda action_item_id=instance.pk,
                requester_id=actor_id,
                was_created=created,
                changed_fields=meaningful,
                should_notify_assignees=notify_assignees: (
                    _enqueue_action_item_notification(
                        "send_action_item_saved_notification",
                        action_item_id,
                        requester_id,
                        was_created,
                        changed_fields,
                        should_notify_assignees,
                    )
                )
            )


    if not created and getattr(instance, '_date_changed', False):
        for action_item in instance.sub_action_items.all():
            child_start, child_end, child_modified = adjust_action_item_dates(
                instance.start_date, 
                instance.target_date,
                action_item.start_date, 
                action_item.target_date
            )
            if child_modified:
                action_item.start_date = child_start
                action_item.target_date = child_end
                action_item.save()


@receiver(m2m_changed, sender=ActionItem.assignees.through)
def action_item_assignees_post_save_operations(sender, instance, action, reverse, pk_set, **kwargs):
    if reverse or not isinstance(instance, ActionItem):
        return

    notification_event = getattr(instance, "_assignee_notification_event", None)
    if action != "post_add":
        return
    if action == "post_add" and not pk_set:
        return

    if getattr(instance, "_suppress_action_item_notifications", False):
        if hasattr(instance, "_assignee_notification_event"):
            delattr(instance, "_assignee_notification_event")
        return

    requester = get_current_user()
    if notification_event in {"create", "update"}:
        transaction.on_commit(
            lambda action_item_id=instance.pk,
            requester_id=getattr(requester, "id", None),
            event=notification_event: _enqueue_action_item_notification(
                "send_action_item_assignee_notification",
                action_item_id,
                requester_id,
                event,
            )
        )
        delattr(instance, "_assignee_notification_event")

@receiver(post_save, sender=Milestone)
def milestone_post_save_operations(sender, instance, created, **kwargs):
    if not created and getattr(instance, '_date_changed', False):
        for action_item in instance.action_items.all():
            child_start, child_end, child_modified = adjust_action_item_dates(
                instance.start_date, 
                instance.due_date,
                action_item.start_date, 
                action_item.target_date
            )
            if child_modified:
                action_item.start_date = child_start
                action_item.target_date = child_end
                action_item.save()

@receiver(post_save, sender=Project)
def create_default_folders(sender, instance, created, **kwargs):
    if not created and getattr(instance, '_date_changed', False):
        for milestone in instance.milestones.all():
            child_start, child_end, child_modified = adjust_milestone_dates(
                instance.project_start_date, 
                instance.project_end_date,
                milestone.start_date, 
                milestone.due_date
            )
            if child_modified:
                milestone.start_date = child_start
                milestone.due_date = child_end
                milestone.save()
    if created:
        creation_locale = constants.resolve_project_template_locale(
            getattr(instance, "_creation_locale", None),
            getattr(instance.created_by, "preferred_language", None),
        )

        # Create ChatRoom for the project team
        ChatRoom.objects.create(name=instance.project_name, project=instance, is_direct=False)

        create_project_configuration(instance)

        project_type = instance.project_type

        if project_type not in ["Angebot", "iSFP", "Dachsanierung"]:
            Milestone.objects.create(
                project=instance,
                name=constants.get_default_project_phase_name(creation_locale),
                start_date=instance.project_start_date,
                due_date=instance.project_end_date,
                reminder_setting="none",  # Set the reminder as specified
                colour=instance.project_colour,  # Set the milestone colour
                status="open",
            )

        # Create root folder for the project
        root_folder = Folder.objects.create(
            project=instance,
            name=f"{instance.id}_root_folder",
            parent=None, 
        )
        
        # Define the list of predefined subfolders
        predefined_folders = [
            "project information",
            "client correspondence",
            "other correspondence",
            "meeting records",
            "invoices",
            "site_reports"
        ]

        # Create predefined folders under the root folder
        for folder_name in predefined_folders:
            Folder.objects.create(
                project=instance,
                name=folder_name,
                parent=root_folder,
            )

        project_user = instance.user

        if project_user:
            freelancer = None
            company = None
            if instance.user.is_freelancer:
                freelancer = instance.user
            else:
                company = instance.user.company
            try:
                default_project_owner_role = Role.objects.get(name="Project Owner", freelancer=freelancer, company=company)
            except Role.DoesNotExist:
                default_project_owner_role = None
            project_team_member, created = ProjectTeamMembers.objects.get_or_create(
                project=instance,
                team_member=project_user,
                role=UserType.PROJECT_MANAGER.value,
                project_role=default_project_owner_role,
                project_access="RW"
            )
            if created and default_project_owner_role:
                UserRole.objects.get_or_create(
                    user=project_user,
                    role=default_project_owner_role,
                    company=company,
                    freelancer=freelancer
                )

        project_config = constants.get_localized_project_template(
            project_type,
            creation_locale,
        )
        if project_config:

            for milestone_data in project_config["milestones"]:
                milestone_due_date = instance.project_start_date + timedelta(days=milestone_data["due_date"])
                
                if instance.project_end_date and milestone_due_date > instance.project_end_date:
                    milestone_due_date = instance.project_end_date
                

                milestone = Milestone.objects.create(
                    project=instance,
                    name=milestone_data["name"],
                    start_date=instance.project_start_date,
                    due_date=milestone_due_date,
                    reminder_setting="2 days",  # Set the reminder as specified
                    colour="#0057B7",  # Set the milestone colour
                    status="open",
                )
                
                # Step 3: Loop through action items for the milestone
                for action_item_data in milestone_data["action_items"]:
                    action_item_due_date = instance.project_start_date + timedelta(days=action_item_data["due_date"])
                    
                    if instance.project_end_date and action_item_due_date > instance.project_end_date:
                        action_item_due_date = instance.project_end_date
                    
                    action_item = ActionItem.objects.create(
                        milestone=milestone,
                        name=action_item_data["name"],
                        start_date=instance.project_start_date,
                        target_date=action_item_due_date,
                        reminder="2 days",
                        status="open",
                    )
                    action_item.assignees.set([project_team_member.team_member])

def get_manager_tm(project, user, managers_dict):
    manager_tm = managers_dict.get(str(project.id))
    if manager_tm is None:
        if project.user.is_freelancer:
            project_owner_role = Role.objects.filter(
                name='Project Owner',
                freelancer=project.user
            ).first()
        else:
            project_owner_role = Role.objects.filter(
                name='Project Owner',
                company=project.company_profile
            ).first()
        manager_tm = ProjectTeamMembers.objects.filter(
            project=project,
            project_role=project_owner_role,
        ).first()
        if manager_tm:
            managers_dict[str(project.id)] = manager_tm
    return manager_tm

#TODO: crate a post_save siganl for Project Team members to mark exisitng messages as read for them
@receiver(pre_delete, sender=ProjectTeamMembers)
def handle_related_objects(sender, instance, **kwargs):
    managers_dict = {}
    updated_items = defaultdict(int)
    project = instance.project
    removed_user = instance.team_member
    removed_by = get_current_user()
    recipients = get_project_recipients(instance.project)
    validated_recipients = get_validate_recipients(recipients, removed_by)
    removed_user_recipients = set()
    if not removed_by:
        removed_by = None
    removed_user_recipients.add(removed_user)
    validated_removed_recipients = get_validate_recipients(removed_user_recipients, removed_by)
    
    actor_name = get_actor_name(removed_by)
    message = format_notification_message(
        'delete',
        f'{removed_user.full_name} was removed from project "{project.project_name}"',
        actor_name
    )

    removed_user_message = [
        f'You have been removed from project "{project.project_name}" by {actor_name}.',
    ]

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ProjectTeamMembers),
        object_id=instance.pk,
        content_object=instance,
        notification_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

    create_notification_with_recipients(
        message=removed_user_message,
        content_type=ContentType.objects.get_for_model(ProjectTeamMembers),
        object_id=instance.pk,
        content_object=instance,
        notification_type='delete',
        recipients=validated_removed_recipients,
        project=instance.project
    )

    # authored_documents
    with transaction.atomic():
        authored_documents = instance.team_member.authored_documents.filter(folder__project=instance.project)
        for document in authored_documents:
            if document.folder.project.user != instance.team_member:
                document.author = document.folder.project.user
                document.save()
                updated_items["documents"] += 1

    # action_items
    with transaction.atomic():
        action_items = instance.team_member.action_items.filter(Q(milestone__project=instance.project) | Q(parent_action_item__milestone__project=instance.project)).distinct()
        for action_item in action_items:
            project = None
            try:
                if action_item.milestone:
                    project = action_item.milestone.project
                else:
                    project = action_item.parent_action_item.milestone.project
            except:
                continue
            
            if project and project.user != instance.team_member:
                action_item.assignees.set([project.user])
                updated_items["action_items"] += 1

    # observed_site_reports
    with transaction.atomic():
        observed_site_reports = instance.observed_site_reports.all()
        for observed_site_report in observed_site_reports:
            manager_tm = get_manager_tm(
                observed_site_report.project,
                observed_site_report.project.user,
                managers_dict,
            )
            if manager_tm and manager_tm != instance:
                observed_site_report.observer = manager_tm
                observed_site_report.save()
                updated_items["observed_site_reports"] += 1

    # presented_site_reports
    with transaction.atomic():
        presented_site_reports = instance.presented_site_reports.all()
        for presented_site_report in presented_site_reports:
            manager_tm = get_manager_tm(
                presented_site_report.project,
                presented_site_report.project.user,
                managers_dict,
            )
            if manager_tm and manager_tm != instance:
                presented_site_report.present.remove(instance)
                presented_site_report.present.add(manager_tm)
                updated_items["presented_site_reports"] += 1

    # distributed_site_reports
    with transaction.atomic():
        distributed_site_reports = instance.distributed_site_reports.all()
        for distributed_site_report in distributed_site_reports:
            manager_tm = get_manager_tm(
                distributed_site_report.project,
                distributed_site_report.project.user,
                managers_dict,
            )
            if manager_tm and manager_tm != instance:
                distributed_site_report.distribution.remove(instance)
                distributed_site_report.distribution.add(manager_tm)
                updated_items["distributed_site_reports"] += 1

    # observations
    with transaction.atomic():
        observations = instance.observations.all()
        for observation in observations:
            manager_tm = get_manager_tm(
                observation.site_report.project,
                observation.site_report.project.user,
                managers_dict,
            )
            if manager_tm and manager_tm != instance:
                observation.contractor = manager_tm
                observation.save()
                updated_items["observations"] += 1

    # Log updates (optional)
    for key, value in updated_items.items():
        print(f"{value} {key} updated.")

def label_for_tm(field_name):
    if field_name in CUSTOM_TEAM_MEMBER_LABELS:
        return CUSTOM_TEAM_MEMBER_LABELS[field_name]
    try:
        field = ProjectTeamMembers._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()
    
@receiver(post_save, sender=ProjectTeamMembers)
def create_related_objects(instance, created, **kwargs):
    project   = instance.project
    member    = instance.team_member
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    project_nm = project.project_name
    user_name = member.full_name
    added_member_recipients = set()

    recipients = get_project_recipients(project)
    added_member_recipients.add(member)
    
    # Get validated recipients and ensure the updated member is excluded from general recipients
    validated_recipients = get_validate_recipients(recipients, requester)
    validated_added_member_recipients = get_validate_recipients(added_member_recipients, requester)
    
    # Remove the added member from the general recipients list to avoid duplicates
    if member in validated_recipients:
        validated_recipients.remove(member)

    if created:
        notification_type = 'create'
        print("Post save for project team member is called.")
        # Create MessageRead records for all existing messages in the project chatroom
        chat_room = ChatRoom.objects.get(project=instance.project)
        messages = chat_room.messages.all()
        # Bulk create records
        message_reads = [
            MessageRead(message=message, user=instance.team_member)
            for message in messages
        ]
        MessageRead.objects.bulk_create(message_reads, ignore_conflicts=True)
        message = f'{user_name} was added to project "{project_nm}" by {actor_name}'
        added_member_messages = [
            f'You have been added to project "{project_nm}" by {actor_name}.',
        ]
    else:
        notification_type = 'update'
        field_changed = instance.tracker.changed()
        meaningful = [
            field for field in field_changed
            if field not in ('updated_at', 'created_at')
        ]
        if not meaningful:
            return

        field_list = ", ".join(label_for_tm(field) for field in meaningful)
        message = f'The project settings for {user_name} were updated in "{project_nm}" by {actor_name} (fields: {field_list})'
        added_member_messages = []

        changed_set = set(meaningful)
        if changed_set == {'role', 'project_access'}:
            new_role   = instance.role
            new_access = instance.project_access
            added_member_messages.append(
                f'Your role was updated to "{new_role}" and your access level to "{new_access}" '
                f'in project "{project_nm}" by {actor_name}.'
            )
        elif 'role' in meaningful:
            new_role = instance.role
            added_member_messages.append(
                f'Your role was updated to "{new_role}" in project "{project_nm}" by {actor_name}.'
            )
        elif 'project_access' in meaningful:
            new_access = instance.project_access
            added_member_messages.append(
                f'Your access level was updated to "{new_access}" in project "{project_nm}" by {actor_name}.'
            )
        elif 'role' in meaningful and 'project_role' in meaningful:
            new_role = instance.role
            new_project_role = instance.project_role
            added_member_messages.append(
                f'Your role was updated to "{new_role}" and your project role was updated to "{new_project_role}" in project "{project_nm}" by {actor_name}.'
            )
        elif 'project_role' in meaningful:
            new_project_role = instance.project_role
            added_member_messages.append(
                f'Your project role was updated to "{new_project_role}" in project "{project_nm}" by {actor_name}.'
            )
        else:
            added_member_messages.append(
                f'You have been updated in project "{project_nm}" by {actor_name}.'
            )
    
    

    # Ensure no duplicate recipients between the two notification groups
    # by removing any overlap between validated_recipients and validated_added_member_recipients
    validated_recipients = validated_recipients - validated_added_member_recipients
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ProjectTeamMembers),
        object_id=instance.pk,
        content_object=instance,
        notification_type='project_team',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project
    )
    create_notification_with_recipients(
        message=added_member_messages,
        content_type=ContentType.objects.get_for_model(ProjectTeamMembers),
        object_id=instance.pk,
        content_object=instance,
        notification_type='project_team',
        action_type=notification_type,
        recipients=validated_added_member_recipients,
        project=instance.project
    )

def label_for_milestone(field_name):
    """Friendly label for a Milestone field."""
    if field_name in CUSTOM_LABELS_MILESTONE:
        return CUSTOM_LABELS_MILESTONE[field_name]
    try:
        field = Milestone._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

@receiver(post_save, sender=Milestone)
def notify_on_milestone_save(sender, instance, created, **kwargs):
    recipients = get_project_recipients(instance.project)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create',
            f'Project Phase "{instance.name or instance.pk}"',
            actor_name,
            project_name=instance.project.project_name
        )
    else:
        notification_type = 'update'
        field_changed = instance.tracker.changed()
        meaningful = [field for field in field_changed if field != 'updated_at']
        if not meaningful:
            return
        labels = ", ".join(label_for_milestone(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Project Phase "{instance.name}"',
            actor_name,
            project_name=instance.project.project_name,
            field_changes=[labels]
        )
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Milestone),
        object_id=instance.pk,
        content_object=instance,
        notification_type="project_phase",
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project
    )

@receiver(pre_delete, sender=Milestone)
def notify_on_milestone_delete(sender, instance, **kwargs):
    recipients = get_project_recipients(instance.project)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    message = format_notification_message(
        'delete',
        f'Project Phase "{instance.name}"',
        actor_name,
        project_name=instance.project.project_name
    )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Milestone),
        object_id=instance.pk,
        content_object=instance,
        notification_type='project_phase',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

def label_for_object_assessment(field_name):
    if field_name in CUSTOM_LABELS_OBJECT_ASSESSMENT:
        return CUSTOM_LABELS_OBJECT_ASSESSMENT[field_name]
    try:
        field = ObjectAssessment._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

def label_for_project_phase(field_name):
    if field_name in CUSTOM_LABELS_PROJECT_PHASE:
        return CUSTOM_LABELS_PROJECT_PHASE[field_name]
    try:
        field = ProjectPhase._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

def label_for_project_funding(field_name):
    if field_name in CUSTOM_LABELS_PROJECT_FUNDING:
        return CUSTOM_LABELS_PROJECT_FUNDING[field_name]
    try:
        field = ProjectFunding._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

def label_for_purchase_order(field_name):
    if field_name in CUSTOM_LABELS_PURCHASE_ORDER:
        return CUSTOM_LABELS_PURCHASE_ORDER[field_name]
    try:
        field = PurchaseOrder._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

@receiver(post_save, sender=ObjectAssessment)
def notify_on_object_assessment_save(sender, instance, created, **kwargs):
    if getattr(instance, "_suppress_object_assessment_notifications", False):
        return

    recipients = get_project_recipients(instance.project)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create',
            f'Object Assessment "{instance.record_title or instance.pk}"',
            actor_name,
            project_name=instance.project.project_name
        )
    else:
        notification_type = 'update'
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f != 'updated_at']
        if not meaningful:
            return
        labels = ", ".join(label_for_object_assessment(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Object Assessment "{instance.record_title}"',
            actor_name,
            project_name=instance.project.project_name,
            field_changes=[labels]
        )
        
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ObjectAssessment),
        object_id=instance.pk,
        content_object=instance,
        notification_type='object_assessment',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project
    )

@receiver(pre_delete, sender=ObjectAssessment)
def notify_on_object_assessment_delete(sender, instance, **kwargs):
    recipients = get_project_recipients(instance.project)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    message = format_notification_message(
        'delete',
        f'Object Assessment "{instance.record_title or instance.pk}"',
        actor_name,
        project_name=instance.project.project_name
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ObjectAssessment),
        object_id=instance.pk,
        content_object=instance,
        notification_type='object_assessment',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

@receiver(post_save, sender=ProjectPhase)
def notify_on_project_phase_save(sender, instance, created, **kwargs):
    recipients = get_project_recipients(instance.project)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create',
            f'Milestone "{instance.name}"',
            actor_name,
            project_name=instance.project.project_name
        )
    else:
        notification_type = 'update'
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        labels = ", ".join(label_for_project_phase(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Milestone "{instance.name}"',
            actor_name,
            project_name=instance.project.project_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ProjectPhase),
        object_id=instance.pk,
        content_object=instance,
        notification_type='milestone',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project
    )

@receiver(pre_delete, sender=ProjectPhase)
def notify_on_project_phase_delete(sender, instance, **kwargs):
    recipients = get_project_recipients(instance.project)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    message = format_notification_message(
        'delete',
        f'Milestone "{instance.name}"',
        actor_name,
        project_name=instance.project.project_name
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ProjectPhase),
        object_id=instance.pk,
        content_object=instance,
        notification_type='milestone',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

@receiver(post_save, sender=ProjectFunding)
def notify_on_project_funding_save(sender, instance, created, **kwargs):
    recipients = get_project_recipients(instance.project)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create',
            f'Funding "{instance.name}"',
            actor_name,
            project_name=instance.project.project_name
        )
    else:
        notification_type = 'update'
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        labels = ", ".join(label_for_project_funding(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Funding "{instance.name}"',
            actor_name,
            project_name=instance.project.project_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ProjectFunding),
        object_id=instance.pk,
        content_object=instance,
        notification_type='project_funding',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project
    )

@receiver(pre_delete, sender=ProjectFunding)
def notify_on_project_funding_delete(sender, instance, **kwargs):
    recipients = get_project_recipients(instance.project)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    message = format_notification_message(
        'delete',
        f'Funding "{instance.name}"',
        actor_name,
        project_name=instance.project.project_name
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ProjectFunding),
        object_id=instance.pk,
        content_object=instance,
        notification_type='project_funding',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

def get_purchase_order_recipients(purchase_order):
    """Get recipients for purchase order notifications across project/company scope."""
    recipients = set()
    if purchase_order.project:
        recipients.update(get_project_recipients(purchase_order.project))
    if purchase_order.company_profile and purchase_order.company_profile.admin_user:
        recipients.add(purchase_order.company_profile.admin_user)
    if purchase_order.freelancer and purchase_order.freelancer.is_freelancer:
        recipients.add(purchase_order.freelancer)
    return recipients

@receiver(post_save, sender=PurchaseOrder)
def notify_on_purchase_order_save(sender, instance, created, **kwargs):
    recipients = get_purchase_order_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    po_display = getattr(instance, "reference", None) or f"PO-{instance.pk}"
    project_name = instance.project.project_name if instance.project else None

    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create',
            f'Purchase Order "{po_display}"',
            actor_name,
            project_name=project_name
        )
    else:
        notification_type = 'update'
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        labels = ", ".join(label_for_purchase_order(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Purchase Order "{po_display}"',
            actor_name,
            project_name=project_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(PurchaseOrder),
        object_id=instance.pk,
        content_object=instance,
        notification_type='purchase_order', 
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project
    )

@receiver(pre_delete, sender=PurchaseOrder)
def notify_on_purchase_order_delete(sender, instance, **kwargs):
    recipients = get_purchase_order_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    po_display = getattr(instance, "reference", None) or f"PO-{instance.pk}"
    message = format_notification_message(
        'delete',
        f'Purchase Order "{po_display}"',
        actor_name,
        project_name=instance.project.project_name if instance.project else None
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(PurchaseOrder),
        object_id=instance.pk,
        content_object=instance,
        notification_type='purchase_order',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

def label_for_document(field_name):
    if field_name in CUSTOM_LABELS_DOCUMENT:
        return CUSTOM_LABELS_DOCUMENT[field_name]
    try:
        field = Document._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()
    
def label_for_folder(field_name):
    if field_name in CUSTOM_LABELS_FOLDER:
        return CUSTOM_LABELS_FOLDER[field_name]
    try:
        field = Folder._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()
    
def label_for_invoice_field(field_name):
    """
    Return a human-readable label for an Invoice field.
    Falls back to the model’s verbose_name or a title-cased version.
    """
    if field_name in CUSTOM_LABELS_INVOICE:
        return CUSTOM_LABELS_INVOICE[field_name]
    try:
        field = Invoice._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

def get_document_recipients(document):
    """Get recipients for document notifications."""
    recipients = set()

    if document.folder.project:
        recipients.update(get_project_recipients(document.folder.project))
    if document.folder.company_profile:
        recipients.add(document.folder.company_profile.admin_user)
    if document.folder.freelancer and document.folder.freelancer.is_freelancer:
        recipients.add(document.folder.freelancer)
    
    return recipients

@receiver(pre_delete, sender=Document)
def notify_on_document_delete(sender, instance, **kwargs):
    recipients = get_document_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    message = format_notification_message(
        'delete',
        f'Document "{instance.file_name}"',
        actor_name
    )
    if instance.author:
        author_recipients = set()
        author_recipients.add(instance.author)
        author_message = [
            f'Your document "{instance.file_name}" has been deleted by {actor_name} with sequence number {instance.sequence_number}.',
        ]
        create_notification_with_recipients(
            message=author_message,
            content_type=ContentType.objects.get_for_model(Document),
            object_id=instance.pk,
            content_object=instance,
            notification_type='documents',
            action_type='delete',
            recipients=author_recipients
        )
    
    if instance.author and instance.author in validated_recipients:
        validated_recipients.remove(instance.author)
        
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Document),
        object_id=instance.pk,
        content_object=instance,
        notification_type='documents',
        action_type='delete',
        recipients=validated_recipients,
        project=None
    )

def get_folder_recipients(folder):
    """Get recipients for folder notifications."""
    recipients = set()

    if folder.project:
        recipients.update(get_project_recipients(folder.project))
    if folder.company_profile:
        recipients.add(folder.company_profile.admin_user)
    if folder.freelancer and folder.freelancer.is_freelancer:
        recipients.add(folder.freelancer)
    return recipients

@receiver(pre_delete, sender=Folder)
def notify_on_folder_delete(sender, instance, **kwargs):
    recipients = get_folder_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    message = format_notification_message(
        'delete',
        f'Folder "{instance.name}"',
        actor_name
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Folder),
        object_id=instance.pk,
        content_object=instance,
        notification_type='folder',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

def get_invoice_recipients(invoice):
    """Get recipients for invoice notifications."""
    recipients = set()
    if invoice.project:
        recipients.update(get_project_recipients(invoice.project))
    if invoice.company_profile:
        recipients.add(invoice.company_profile.admin_user)
    if invoice.freelancer and invoice.freelancer.is_freelancer:
        recipients.add(invoice.freelancer)
    return recipients

@receiver(pre_delete, sender=Invoice)
def notify_on_invoice_delete(sender, instance, **kwargs):
    recipients = get_invoice_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if instance.reciepient_user:
        name = instance.reciepient_user.name
    elif instance.reciepient_company:
        name = instance.reciepient_company.company_name
    else:
        name = "Unknown Recipient"
    
    message = format_notification_message(
        'delete',
        f'Invoice for "{name}"',
        actor_name
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Invoice),
        object_id=instance.pk,
        content_object=instance,
        notification_type='invoice',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

@receiver(post_save, sender=Document)
def notify_on_document_save(sender, instance, created, **kwargs):
    from project_management.services.document_indexing import enqueue_document_indexing

    if created or instance.tracker.has_changed("file"):
        transaction.on_commit(lambda: enqueue_document_indexing(instance.id))

    if getattr(instance, "_suppress_document_notifications", False):
        return

    recipients = get_document_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return
    
    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create',
            f'Document "{instance.file_name or instance.file.name}"',
            actor_name,
            project_name=f'folder "{instance.folder.name}"'
        )
        if instance.author:
            author_recipients = set()
            author_recipients.add(instance.author)
            author_message = [
                f'Your document "{instance.file_name or instance.file.name}" has been created with sequence number {instance.sequence_number}.',
            ]
            create_notification_with_recipients(
                message=author_message,
                content_type=ContentType.objects.get_for_model(Document),
                object_id=instance.pk,
                content_object=instance,
                notification_type='documents',
                action_type='create',
                recipients=author_recipients,
                project=instance.folder.project if instance.folder and instance.folder.project else None
            )
    else:
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        notification_type = 'update'
        labels = ", ".join(label_for_document(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Document "{instance.file_name or instance.file.name}"',
            actor_name,
            project_name=f'folder "{instance.folder.name}"',
            field_changes=[labels]
        )
        if instance.author:
            author_recipients = set()
            author_recipients.add(instance.author)
            author_message = [
                f'Your document "{instance.file_name or instance.file.name}" has been updated with sequence number {instance.sequence_number}.',
            ]
            create_notification_with_recipients(
                message=author_message,
                content_type=ContentType.objects.get_for_model(Document),
                object_id=instance.pk,
                content_object=instance,
                notification_type='documents',
                action_type=notification_type,
                recipients=author_recipients,
                project=instance.folder.project if instance.folder and instance.folder.project else None
            )

    if instance.author and instance.author in validated_recipients:
        validated_recipients.remove(instance.author)

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Document),
        object_id=instance.pk,
        content_object=instance,
        notification_type='documents',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.folder.project if instance.folder and instance.folder.project else None,
    )

@receiver(post_save, sender=Folder)
def notify_on_folder_save(sender, instance, created, **kwargs):
    recipients = get_folder_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return

    if created:
        notification_type = 'create'
        project_name = instance.project.project_name if instance.project else None
        message = format_notification_message(
            'create',
            f'Folder "{instance.name}"',
            actor_name,
            project_name=project_name
        )
    else:
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        notification_type = 'update'
        labels = ", ".join(label_for_folder(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Folder "{instance.name}"',
            actor_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Folder),
        object_id=instance.pk,
        content_object=instance,
        notification_type='folder',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project if instance.project else None,
    )

@receiver(post_save, sender=Invoice)
def notify_on_invoice_save(sender, instance, created, **kwargs):
    recipients = get_invoice_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return

    if instance.reciepient_user:
        name = instance.reciepient_user.name
    elif instance.reciepient_company:
        name = instance.reciepient_company.company_name
    else:
        name = "Unknown Recipient"

    if created:
        notification_type = 'create'
        project_name = instance.project.project_name if instance.project else None
        message = format_notification_message(
            'create',
            f'Invoice for "{name}"',
            actor_name,
            project_name=project_name
        )
    else:
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        notification_type = 'update'
        labels = ", ".join(label_for_invoice_field(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Invoice for "{name}"',
            actor_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(Invoice),
        object_id=instance.pk,
        content_object=instance,
        notification_type='invoice',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project,
    )

def label_for_timecard_field(field_name):
    """
    Return a human-readable label for a TimeCard field.
    Falls back to the model's verbose_name or a title-cased version.
    """
    if field_name in CUSTOM_LABELS_TIMECARD:
        return CUSTOM_LABELS_TIMECARD[field_name]
    try:
        field = TimeCard._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

def label_for_expensecard_field(field_name):
    """
    Return a human-readable label for an ExpenseCard field.
    Falls back to the model's verbose_name or a title-cased version.
    """
    if field_name in CUSTOM_LABELS_EXPENSECARD:
        return CUSTOM_LABELS_EXPENSECARD[field_name]
    try:
        field = ExpenseCard._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

def get_time_expense_recipients(instance):
    """Get recipients for time/expense card notifications."""
    recipients = set()
    requester = get_current_user()
    if requester:
        recipients.add(requester)
    if instance.created_by:
        recipients.add(instance.created_by)
    if instance.project:
        recipients.update(get_project_recipients(instance.project))
    if instance.company_profile:
        recipients.add(instance.company_profile.admin_user)
    if instance.freelancer and instance.freelancer.is_freelancer:
        recipients.add(instance.freelancer)
    return recipients

@receiver(post_save, sender=TimeCard)
def notify_timecard_save(sender, instance, created, **kwargs):
    recipients = get_time_expense_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return

    if created:
        notification_type = 'create'
        project_name = instance.project.project_name if instance.project else None
        message = format_notification_message(
            'create',
            f'TimeCard "{instance.name}"',
            actor_name,
            project_name=project_name
        )
    else:
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at', 'total')]
        if not meaningful:
            return
        notification_type = 'update'
        labels = ", ".join(label_for_timecard_field(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'TimeCard "{instance.name}"',
            actor_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(TimeCard),
        object_id=instance.pk,
        content_object=instance,
        notification_type='time_card',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project if instance.project else None
    )

@receiver(pre_delete, sender=TimeCard)
def notify_timecard_delete(sender, instance, **kwargs):
    recipients = get_time_expense_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return

    if instance.freelancer:
        name = instance.freelancer.full_name
    elif instance.project:
        name = instance.project.project_name
    elif instance.company_profile:
        name = instance.company_profile.company_name
    else:
        name = "Unknown Client"

    message = format_notification_message(
        'delete',
        f'TimeCard "{instance.name}" for "{name}"',
        actor_name
    )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(TimeCard),
        object_id=instance.pk,
        content_object=instance,
        notification_type='time_card',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project if instance.project else None
    )

@receiver(post_save, sender=ExpenseCard)
def notify_expensecard_save(sender, instance, created, **kwargs):
    recipients = get_time_expense_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return

    if created:
        notification_type = 'create'
        project_name = instance.project.project_name if instance.project else None
        message = format_notification_message(
            'create',
            f'ExpenseCard "{instance.name}"',
            actor_name,
            project_name=project_name
        )
    else:
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at', 'total')]
        if not meaningful:
            return

        if 'status' in meaningful and instance.status in ('approved', 'rejected'):
            owner_recipients = set()
            if instance.created_by:
                owner_recipients.add(instance.created_by)
            validated_owner_recipients = get_validate_recipients(
                owner_recipients,
                requester,
            )
            if not validated_owner_recipients:
                return

            action_label = 'approved' if instance.status == 'approved' else 'rejected'
            message = (
                f'Expense Card "{instance.name}" was {action_label} by {actor_name}.'
            )

            create_notification_with_recipients(
                message=message,
                content_type=ContentType.objects.get_for_model(ExpenseCard),
                object_id=instance.pk,
                content_object=instance,
                notification_type='time_expense',
                action_type='update',
                recipients=validated_owner_recipients,
                project=instance.project if instance.project else None,
                bypass_preference_check=True,
            )
            return

        notification_type = 'update'
        labels = ", ".join(label_for_expensecard_field(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'ExpenseCard "{instance.name}"',
            actor_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ExpenseCard),
        object_id=instance.pk,
        content_object=instance,
        notification_type='time_expense',
        action_type=notification_type,
        recipients=validated_recipients,
        project=instance.project if instance.project else None
    )

@receiver(pre_delete, sender=ExpenseCard)
def notify_expensecard_delete(sender, instance, **kwargs):
    recipients = get_time_expense_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return

    if instance.freelancer:
        name = instance.freelancer.full_name
    elif instance.project:
        name = instance.project.project_name
    elif instance.company_profile:
        name = instance.company_profile.company_name
    else:
        name = "Unknown Client"

    message = format_notification_message(
        'delete',
        f'ExpenseCard "{instance.name}" for "{name}"',
        actor_name
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ExpenseCard),
        object_id=instance.pk,
        content_object=instance,
        notification_type='time_expense',
        action_type='delete',
        recipients=validated_recipients,
        project=instance.project
    )

def get_contact_recipients(instance):
    recipients = set()
    if instance.company_profile:
        recipients.add(instance.company_profile.admin_user)
    if instance.user:
        recipients.add(instance.user)
    return recipients

def label_for_contact_user_field(field_name):
    if field_name in CUSTOM_LABELS_CONTACT_USER:
        return CUSTOM_LABELS_CONTACT_USER[field_name]
    try:
        field = ContactUser._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()

@receiver(post_save, sender=ContactUser)
def notify_on_contact_save(sender, instance, created, **kwargs):
    recipients = get_contact_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return
    
    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create',
            f'Contact "{instance.name}"',
            actor_name,
            project_name="Not Assigned"
        )
    else:
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        notification_type = 'update'
        labels = ", ".join(label_for_contact_user_field(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Contact "{instance.name}"',
            actor_name,
            field_changes=[labels]
        )

    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ContactUser),
        object_id=instance.pk,
        content_object=instance,
        notification_type='contact_user',
        action_type=notification_type,
        recipients=validated_recipients,
    )

@receiver(pre_delete, sender=ContactUser)
def notify_on_contact_user_delete(sender, instance, **kwargs):
    recipients = get_contact_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return
    
    message = format_notification_message(
        'delete',
        f'Contact "{instance.name}"',
        actor_name
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ContactUser),
        object_id=instance.pk,
        content_object=instance,
        notification_type='contact_user',
        action_type='delete',
        recipients=validated_recipients,
        project=None
    )

def label_for_contact_company_field(field_name):
    if field_name in CUSTOM_LABELS_CONTACT_COMPANY:
        return CUSTOM_LABELS_CONTACT_COMPANY[field_name]
    try:
        field = ContactCompany._meta.get_field(field_name)
        return capfirst(field.verbose_name)
    except FieldDoesNotExist:
        return field_name.replace('_', ' ').title()



@receiver(post_save, sender=ContactCompany)
def notify_on_contact_company_save(sender, instance, created, **kwargs):
    recipients = get_contact_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    if created:
        notification_type = 'create'
        message = format_notification_message(
            'create',
            f'Contact Company "{instance.company_name}"',
            actor_name,
            project_name="Not Assigned"
        )
    else:
        changed = instance.tracker.changed()
        meaningful = [f for f in changed if f not in ('updated_at',)]
        if not meaningful:
            return
        notification_type = 'update'
        labels = ", ".join(label_for_contact_company_field(f) for f in meaningful)
        message = format_notification_message(
            'update',
            f'Contact Company "{instance.company_name}"',
            actor_name,
            field_changes=[labels]
        )

    
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ContactCompany),
        object_id=instance.pk,
        content_object=instance,
        notification_type='contact_company',
        action_type=notification_type,
        recipients=validated_recipients,
        project=None
    )

@receiver(pre_delete, sender=ContactCompany)
def notify_on_contact_company_delete(sender, instance, **kwargs):
    recipients = get_contact_recipients(instance)
    requester = get_current_user()
    actor_name = get_actor_name(requester)
    validated_recipients = get_validate_recipients(recipients, requester)
    
    if not recipients:
        return
    
    message = format_notification_message(
        'delete',
        f'Contact Company "{instance.company_name}"',
        actor_name
    )
    
    create_notification_with_recipients(
        message=message,
        content_type=ContentType.objects.get_for_model(ContactCompany),
        object_id=instance.pk,
        content_object=instance,
        notification_type='contact_company',
        action_type='delete',
        recipients=validated_recipients,
        project=None
    )


ADVANCED_SEARCH_SOURCE_TYPES = {
    Document: SearchRecord.SourceType.DOCUMENT,
    ObjectAssessment: SearchRecord.SourceType.OBJECT_ASSESSMENT,
    SolutionDemand: SearchRecord.SourceType.SOLUTION_DEMAND,
    Requirement: SearchRecord.SourceType.REQUIREMENT,
    AvailableProductPart: SearchRecord.SourceType.AVAILABLE_PRODUCT_PART,
    Machine: SearchRecord.SourceType.MACHINE,
    Equipment: SearchRecord.SourceType.EQUIPMENT,
    BillOfMaterialPart: SearchRecord.SourceType.BOM_PART,
    CompanySolutionSpaceItem: SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
    ProjectSolutionSpaceItem: SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
    PurchaseOrder: SearchRecord.SourceType.PURCHASE_ORDER,
    Invoice: SearchRecord.SourceType.INVOICE,
    ActionItemComment: SearchRecord.SourceType.ACTION_ITEM_COMMENT,
    MatrixComment: SearchRecord.SourceType.MATRIX_COMMENT,
}


@receiver(post_save)
def enqueue_advanced_search_record(sender, instance, raw=False, **kwargs):
    source_type = ADVANCED_SEARCH_SOURCE_TYPES.get(sender)
    if not source_type or raw:
        return
    from project_management.services.search_indexing import enqueue_search_indexing

    transaction.on_commit(
        lambda: enqueue_search_indexing(source_type, instance.pk)
    )


@receiver(pre_delete)
def remove_advanced_search_record(sender, instance, **kwargs):
    source_type = ADVANCED_SEARCH_SOURCE_TYPES.get(sender)
    if not source_type:
        return
    SearchRecord.objects.filter(
        source_type=source_type,
        source_id=instance.pk,
    ).delete()


ADVANCED_SEARCH_M2M_MODELS = {
    ActionItem.documents.through: {
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    ObjectAssessment.documents.through: {
        ObjectAssessment: SearchRecord.SourceType.OBJECT_ASSESSMENT,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    SolutionDemand.documents.through: {
        SolutionDemand: SearchRecord.SourceType.SOLUTION_DEMAND,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    SolutionDemand.object_assessments.through: {
        SolutionDemand: SearchRecord.SourceType.SOLUTION_DEMAND,
        ObjectAssessment: SearchRecord.SourceType.OBJECT_ASSESSMENT,
    },
    SolutionDemand.linked_folders.through: {
        SolutionDemand: SearchRecord.SourceType.SOLUTION_DEMAND,
    },
    Requirement.documents.through: {
        Requirement: SearchRecord.SourceType.REQUIREMENT,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    Requirement.object_assessments.through: {
        Requirement: SearchRecord.SourceType.REQUIREMENT,
        ObjectAssessment: SearchRecord.SourceType.OBJECT_ASSESSMENT,
    },
    Requirement.solution_demands.through: {
        Requirement: SearchRecord.SourceType.REQUIREMENT,
        SolutionDemand: SearchRecord.SourceType.SOLUTION_DEMAND,
    },
    Requirement.linked_folders.through: {
        Requirement: SearchRecord.SourceType.REQUIREMENT,
    },
    Transport.documents.through: {
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    Transport.object_assessments.through: {
        ObjectAssessment: SearchRecord.SourceType.OBJECT_ASSESSMENT,
    },
    AvailableProductPart.documents.through: {
        AvailableProductPart: SearchRecord.SourceType.AVAILABLE_PRODUCT_PART,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    AvailableProductPart.object_assessments.through: {
        AvailableProductPart: SearchRecord.SourceType.AVAILABLE_PRODUCT_PART,
        ObjectAssessment: SearchRecord.SourceType.OBJECT_ASSESSMENT,
    },
    AvailableProductPart.linked_folders.through: {
        AvailableProductPart: SearchRecord.SourceType.AVAILABLE_PRODUCT_PART,
    },
    Machine.documents.through: {
        Machine: SearchRecord.SourceType.MACHINE,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    Machine.projects.through: {
        Machine: SearchRecord.SourceType.MACHINE,
    },
    Machine.linked_folders.through: {
        Machine: SearchRecord.SourceType.MACHINE,
    },
    Equipment.documents.through: {
        Equipment: SearchRecord.SourceType.EQUIPMENT,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    Equipment.linked_folders.through: {
        Equipment: SearchRecord.SourceType.EQUIPMENT,
    },
    BillOfMaterialPart.documents.through: {
        BillOfMaterialPart: SearchRecord.SourceType.BOM_PART,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    BillOfMaterialPart.object_assessments.through: {
        BillOfMaterialPart: SearchRecord.SourceType.BOM_PART,
        ObjectAssessment: SearchRecord.SourceType.OBJECT_ASSESSMENT,
    },
    BillOfMaterialPart.requirements.through: {
        BillOfMaterialPart: SearchRecord.SourceType.BOM_PART,
        Requirement: SearchRecord.SourceType.REQUIREMENT,
    },
    BillOfMaterialPart.linked_folders.through: {
        BillOfMaterialPart: SearchRecord.SourceType.BOM_PART,
    },
    CompanySolutionSpaceItem.documents.through: {
        CompanySolutionSpaceItem: SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    CompanySolutionSpaceItem.projects.through: {
        CompanySolutionSpaceItem: SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
    },
    CompanySolutionSpaceItem.customers.through: {
        CompanySolutionSpaceItem: SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
    },
    CompanySolutionSpaceItem.linked_folders.through: {
        CompanySolutionSpaceItem: SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
    },
    ProjectSolutionSpaceItem.documents.through: {
        ProjectSolutionSpaceItem: SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    ProjectSolutionSpaceItem.object_assessments.through: {
        ProjectSolutionSpaceItem: SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
        ObjectAssessment: SearchRecord.SourceType.OBJECT_ASSESSMENT,
    },
    ProjectSolutionSpaceItem.requirements.through: {
        ProjectSolutionSpaceItem: SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
        Requirement: SearchRecord.SourceType.REQUIREMENT,
    },
    ProjectSolutionSpaceItem.customers.through: {
        ProjectSolutionSpaceItem: SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
    },
    ProjectSolutionSpaceItem.linked_folders.through: {
        ProjectSolutionSpaceItem: SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
    },
    PurchaseOrder.documents.through: {
        PurchaseOrder: SearchRecord.SourceType.PURCHASE_ORDER,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
    PurchaseOrder.linked_folders.through: {
        PurchaseOrder: SearchRecord.SourceType.PURCHASE_ORDER,
    },
    Invoice.documents.through: {
        Invoice: SearchRecord.SourceType.INVOICE,
        Document: SearchRecord.SourceType.DOCUMENT,
    },
}


def _advanced_search_relation_ids(sender, instance, model):
    instance_field = next(
        (
            field
            for field in sender._meta.fields
            if getattr(field, "related_model", None) is instance.__class__
        ),
        None,
    )
    related_field = next(
        (
            field
            for field in sender._meta.fields
            if getattr(field, "related_model", None) is model
        ),
        None,
    )
    if not instance_field or not related_field:
        return []
    return list(
        sender.objects.filter(**{instance_field.attname: instance.pk}).values_list(
            related_field.attname,
            flat=True,
        )
    )


@receiver(m2m_changed)
def enqueue_advanced_search_relation_change(
    sender,
    instance,
    action,
    reverse=False,
    pk_set=None,
    model=None,
    **kwargs,
):
    model_source_types = ADVANCED_SEARCH_M2M_MODELS.get(sender)
    if not model_source_types:
        return
    cache_key = f"_advanced_search_clear_ids_{sender._meta.label_lower.replace('.', '_')}"
    if action == "pre_clear":
        setattr(
            instance,
            cache_key,
            _advanced_search_relation_ids(sender, instance, model),
        )
        return
    if action not in {"post_add", "post_remove", "post_clear"}:
        return
    from project_management.services.search_indexing import enqueue_search_indexing

    related_ids = list(pk_set or getattr(instance, cache_key, []))
    records = []
    instance_source_type = model_source_types.get(instance.__class__)
    if instance_source_type:
        records.append((instance_source_type, instance.pk))
    related_source_type = model_source_types.get(model)
    if related_source_type:
        records.extend((related_source_type, related_id) for related_id in related_ids)
    records = list(dict.fromkeys(records))
    transaction.on_commit(
        lambda: [
            enqueue_search_indexing(source_type, source_id)
            for source_type, source_id in records
        ]
    )


@receiver(post_save, sender=ProjectMatrixConfig)
@receiver(post_save, sender=PartsListConfig)
@receiver(post_save, sender=SolutionSpaceConfig)
@receiver(post_save, sender=MachineConfig)
@receiver(post_save, sender=EquipmentConfig)
@receiver(pre_delete, sender=ProjectMatrixConfig)
@receiver(pre_delete, sender=PartsListConfig)
@receiver(pre_delete, sender=SolutionSpaceConfig)
@receiver(pre_delete, sender=MachineConfig)
@receiver(pre_delete, sender=EquipmentConfig)
def reindex_advanced_search_configuration(sender, instance, raw=False, **kwargs):
    if raw:
        return
    from project_management.services.search_indexing import enqueue_search_indexing

    source_pairs = []
    if sender is ProjectMatrixConfig:
        source_pairs = [
            (SearchRecord.SourceType.SOLUTION_DEMAND, SolutionDemand.objects.filter(project_id=instance.project_id)),
            (SearchRecord.SourceType.REQUIREMENT, Requirement.objects.filter(project_id=instance.project_id)),
        ]
    elif sender is PartsListConfig:
        source_pairs = [
            (
                SearchRecord.SourceType.BOM_PART,
                BillOfMaterialPart.objects.filter(project_id=instance.project_id),
            )
        ] if instance.project_id else [
            (
                SearchRecord.SourceType.AVAILABLE_PRODUCT_PART,
                AvailableProductPart.objects.filter(company_profile_id=instance.company_profile_id),
            )
        ]
    elif sender is SolutionSpaceConfig:
        source_pairs = [
            (
                SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
                ProjectSolutionSpaceItem.objects.filter(project_id=instance.project_id),
            )
        ] if instance.project_id else [
            (
                SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
                CompanySolutionSpaceItem.objects.filter(company_profile_id=instance.company_profile_id),
            )
        ]
    elif sender is MachineConfig:
        source_pairs = [
            (
                SearchRecord.SourceType.MACHINE,
                Machine.objects.filter(company_profile_id=instance.company_profile_id),
            )
        ]
    elif sender is EquipmentConfig:
        source_pairs = [
            (
                SearchRecord.SourceType.EQUIPMENT,
                Equipment.objects.filter(company_profile_id=instance.company_profile_id),
            )
        ]

    records = [
        (source_type, source_id)
        for source_type, queryset in source_pairs
        for source_id in queryset.values_list("id", flat=True)
    ]
    transaction.on_commit(
        lambda: [enqueue_search_indexing(source_type, source_id) for source_type, source_id in records]
    )


@receiver(post_save, sender=PurchaseOrderLineItem)
@receiver(pre_delete, sender=PurchaseOrderLineItem)
def reindex_purchase_order_line_item(sender, instance, raw=False, **kwargs):
    if raw:
        return
    from project_management.services.search_indexing import enqueue_search_indexing

    transaction.on_commit(
        lambda: enqueue_search_indexing(
            SearchRecord.SourceType.PURCHASE_ORDER,
            instance.purchase_order_id,
        )
    )


@receiver(post_save, sender=InvoiceLineItem)
@receiver(pre_delete, sender=InvoiceLineItem)
def reindex_invoice_line_item(sender, instance, raw=False, **kwargs):
    if raw:
        return
    from project_management.services.search_indexing import enqueue_search_indexing

    transaction.on_commit(
        lambda: enqueue_search_indexing(
            SearchRecord.SourceType.INVOICE,
            instance.invoice_id,
        )
    )


@receiver(post_save, sender=PurchaseOrder)
@receiver(pre_delete, sender=PurchaseOrder)
def reindex_purchase_order_parent(sender, instance, raw=False, **kwargs):
    if raw or not instance.source_po_id:
        return
    from project_management.services.search_indexing import enqueue_search_indexing

    transaction.on_commit(
        lambda: enqueue_search_indexing(
            SearchRecord.SourceType.PURCHASE_ORDER,
            instance.source_po_id,
        )
    )


@receiver(post_save, sender=Invoice)
@receiver(pre_delete, sender=Invoice)
def reindex_invoice_parents(sender, instance, raw=False, **kwargs):
    if raw:
        return
    from project_management.services.search_indexing import enqueue_search_indexing

    records = []
    if instance.purchase_order_id:
        records.append((SearchRecord.SourceType.PURCHASE_ORDER, instance.purchase_order_id))
    if instance.source_invoice_id:
        records.append((SearchRecord.SourceType.INVOICE, instance.source_invoice_id))
    if instance.replaced_invoice_id:
        records.append((SearchRecord.SourceType.INVOICE, instance.replaced_invoice_id))
    if not records:
        return
    transaction.on_commit(
        lambda: [
            enqueue_search_indexing(source_type, source_id)
            for source_type, source_id in records
        ]
    )


# NETWORK-CONTACT-FIX-02: ProjectExternalUsers.contact_user / contact_company are SET_NULL, so
# physically deleting a contact used to leave a link with NEITHER contact -- a nameless external
# member that project screens re-post and the project serializer then rejects. A soft delete
# (is_deleted) never reaches here and keeps its links (the contact stays restorable); only a real
# delete removes the links that would become orphans (a link that still points to the other kind
# of contact is kept).
@receiver(pre_delete, sender=ContactUser)
def remove_orphan_project_links_of_deleted_contact_user(sender, instance, **kwargs):
    ProjectExternalUsers.objects.filter(
        contact_user=instance, contact_company__isnull=True
    ).delete()


@receiver(pre_delete, sender=ContactCompany)
def remove_orphan_project_links_of_deleted_contact_company(sender, instance, **kwargs):
    ProjectExternalUsers.objects.filter(
        contact_company=instance, contact_user__isnull=True
    ).delete()
