from django.db.models import Q
from chat.models import ChatRoom
from project_management.utils import (
    has_accepted_connection,
    has_accepted_internal_user_connection,
)


def users_share_direct_chat_project(user1, user2):
    if user1.id == user2.id:
        return True

    user1_projects = set(user1.project_teams.values_list("project_id", flat=True))
    user2_projects = set(user2.project_teams.values_list("project_id", flat=True))
    return bool(user1_projects & user2_projects)


def can_open_direct_chat(user1, user2):
    if user1.id == user2.id:
        return False

    if users_share_direct_chat_project(user1, user2):
        return True

    return are_users_connected(user1, user2)

def get_user_chat_rooms(user):
    """
        This function retrieves the chat rooms for a user.
    """
    user_filter = Q()

    if user.is_freelancer:
        user_filter = (
            Q(freelancer=user) |
            Q(project__user=user) |
            Q(project__team_members__team_member=user) |
            (Q(is_direct=True) & Q(participants=user))
        )
    else:
        user_filter = (
            Q(company=user.company) |
            Q(project__company_profile=user.company) |
            Q(project__team_members__team_member=user) |
            (Q(is_direct=True) & Q(participants=user))
        )

    rooms = set(ChatRoom.objects.filter(user_filter).values_list('id', flat=True))

    group_rooms = ChatRoom.objects.filter(is_group=True, participants=user)
    for group_room in group_rooms:
        if can_access_group_chat(user, group_room):
            rooms.add(group_room.id)

    return list(rooms)


def find_direct_chat_room(user_a, user_b):
    if user_a.id == user_b.id:
        return None

    return (
        ChatRoom.objects.filter(is_direct=True)
        .filter(participants=user_a)
        .filter(participants=user_b)
        .distinct()
        .first()
    )


def are_users_connected(user1, user2):
    if user1.id == user2.id:
        return True

    # both freelancers
    if user1.is_freelancer and user2.is_freelancer:
        return has_accepted_connection(user1.sharemundo_links_id, user2.sharemundo_links_id)

    # freelancer <-> company user
    if user1.is_freelancer and user2.company:
        return has_accepted_connection(user2.company.sharemundo_links_id, user1.sharemundo_links_id)
    if user2.is_freelancer and user1.company:
        return has_accepted_connection(user1.company.sharemundo_links_id, user2.sharemundo_links_id)

    # company users in same company
    if user1.company and user2.company and user1.company == user2.company:
        return True

    # company users in different companies
    if user1.company and user2.company and user1.company != user2.company:
        return has_accepted_internal_user_connection(
            user1.company.sharemundo_links_id,
            user2.company.sharemundo_links_id,
            user2.id,
        ) or has_accepted_internal_user_connection(
            user2.company.sharemundo_links_id,
            user1.company.sharemundo_links_id,
            user1.id,
        )

    return False


def can_access_group_chat(user, room):
    if not room.is_group:
        return True

    if not room.participants.filter(id=user.id).exists():
        return False

    owner = room.created_by
    if not owner:
        return True

    if owner.id == user.id:
        return True

    return are_users_connected(owner, user)

def has_access_to_messages(user, messages):
    """
        This function checks if the user has access to all of the messages.
    """
    user_rooms = get_user_chat_rooms(user)
    return all(room in user_rooms for room in messages.values_list('room', flat=True))
