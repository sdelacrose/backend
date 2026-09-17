
from rest_framework.response import Response
from django.db import transaction
from rest_framework.views import APIView
from rest_framework import status
from django.conf import settings

from models.models import ConnectionUser, CompanyProfile, User, Staff, Role, UserRole, Connection
from project_management.models import (
    ActionItem, Project, ProjectTeamMembers, ProjectTime, Folder
)
from chat.models import ChatRoom, MessageRead


class UpdateFreelancerProjectOnConnectionRevoke(APIView):
    """
    API view to handle freelancer project updates when a Connection is revoked.
    This handles direct Connection objects for freelancer connections.
    Business logic: company-to-freelancer and freelancer to freelancer connections are possible.
    """
    permission_classes = []

    def post(self, request, *args, **kwargs):
        token = request.data.get("SECRET_TOKEN")
        # Check if the token is missing
        if not token:
            return Response({"error": "Authorization token is required."}, status=status.HTTP_401_UNAUTHORIZED)

        # Validate the token
        if token != settings.API_SECRET_KEY:
            return Response({"error": "Invalid authorization token."}, status=status.HTTP_403_FORBIDDEN)

        connection_id = request.data.get("connection_id")
        if not connection_id:
            return Response({"error": "Invalid payload."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            connection = Connection.objects.get(id=int(connection_id))
            # Business logic: only company-to-freelancer connections are possible
            if connection.initiator_type == 'company' and connection.target_type == 'freelancer':
                # Company-to-freelancer connection handling
                company = CompanyProfile.objects.get(sharemundo_links_id=connection.initiator_id)
                freelancer = User.objects.get(sharemundo_links_id=connection.target_id)
                
                # Get company projects with optimized queries
                company_projects = Project.objects.filter(company_profile=company).prefetch_related(
                    'team_members__team_member',
                    'milestones__action_items'
                )
                
                freelancer_team_memberships = ProjectTeamMembers.objects.filter(
                    project__in=company_projects,
                    team_member=freelancer
                ).select_related('project')
                
                projects_affected = []
                
                # Process each team membership
                for team_member in freelancer_team_memberships:
                    project = team_member.project
                    projects_affected.append(project.id)
                    
                    # Reassign action items back to project owner
                    affected_action_items = ActionItem.objects.filter(
                        assignees=freelancer,
                        milestone__project=project
                    ).distinct()
                    for action_item in affected_action_items:
                        action_item.assignees.set([project.user])
                
                # Batch update project times for all affected projects
                if projects_affected:
                    for project_id in projects_affected:
                        ProjectTime.objects.filter(
                            project_id=project_id, 
                            user=freelancer
                        ).update(user_id=Project.objects.get(id=project_id).user_id)
                
                # Delete freelancer team memberships from company projects
                freelancer_team_memberships.delete()
                
                print(f"[DEBUG] Removed freelancer {freelancer.id} from {len(projects_affected)} company projects")
                
            elif connection.initiator_type == 'freelancer' and connection.target_type == 'freelancer':
                # Freelancer-to-freelancer connection handling
                freelancer1 = User.objects.get(sharemundo_links_id=connection.initiator_id)
                freelancer2 = User.objects.get(sharemundo_links_id=connection.target_id)
                
                # Verify both users are freelancers
                if not freelancer1.is_freelancer or not freelancer2.is_freelancer:
                    return Response({
                        "error": "Both users must be freelancers for freelancer-to-freelancer connections."
                    }, status=status.HTTP_400_BAD_REQUEST)
                
                # Get projects where each freelancer is the OWNER
                freelancer1_owned_projects = Project.objects.filter(user=freelancer1)
                freelancer2_owned_projects = Project.objects.filter(user=freelancer2)
                
                # Remove freelancer2 from freelancer1's projects
                freelancer2_removed_count = 0
                for project in freelancer1_owned_projects:
                    # Remove freelancer2 from freelancer1's project team
                    ProjectTeamMembers.objects.filter(
                        project=project,
                        team_member=freelancer2
                    ).delete()
                    
                    # Reassign freelancer2's action items back to project owner (freelancer1)
                    affected_action_items = ActionItem.objects.filter(
                        assignees=freelancer2,
                        milestone__project=project
                    ).distinct()
                    for action_item in affected_action_items:
                        action_item.assignees.set([project.user])
                    
                    # Update freelancer2's project times back to project owner (freelancer1)
                    ProjectTime.objects.filter(project=project, user=freelancer2).update(user=freelancer1)
                    
                    freelancer2_removed_count += 1
                
                # Remove freelancer1 from freelancer2's projects
                freelancer1_removed_count = 0
                for project in freelancer2_owned_projects:
                    # Remove freelancer1 from freelancer2's project team
                    ProjectTeamMembers.objects.filter(
                        project=project,
                        team_member=freelancer1
                    ).delete()
                    
                    # Reassign freelancer1's action items back to project owner (freelancer2)
                    affected_action_items = ActionItem.objects.filter(
                        assignees=freelancer1,
                        milestone__project=project
                    ).distinct()
                    for action_item in affected_action_items:
                        action_item.assignees.set([project.user])
                    
                    # Update freelancer1's project times back to project owner (freelancer2)
                    ProjectTime.objects.filter(project=project, user=freelancer1).update(user=freelancer2)
                    
                    freelancer1_removed_count += 1
                
                print(f"[DEBUG] Removed freelancer2 from {freelancer2_removed_count} of freelancer1's projects")
                print(f"[DEBUG] Removed freelancer1 from {freelancer1_removed_count} of freelancer2's projects")
                    
            else:
                return Response({
                    "error": "Invalid connection type. Only company-to-freelancer and freelancer-to-freelancer connections are supported."
                }, status=status.HTTP_400_BAD_REQUEST)

            return Response({"message": "Connection projects updated successfully."}, status=status.HTTP_200_OK)
            
        except Connection.DoesNotExist:
            return Response({"error": "Connection not found."}, status=status.HTTP_404_NOT_FOUND)
        except CompanyProfile.DoesNotExist:
            return Response({"error": "Company not found."}, status=status.HTTP_404_NOT_FOUND)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": "Internal server error occurred."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UpdateProjectOnConnectionRevoke(APIView):
    """
    API view to handle project updates when a connection is revoked.
    This bypasses default JWT validation and manually validates the token.
    """
    permission_classes = []

    def post(self, request, *args, **kwargs):
        token = request.data.get("SECRET_TOKEN")
        # Check if the token is missing
        if not token:
            return Response({"error": "Authorization token is required."}, status=status.HTTP_401_UNAUTHORIZED)

        # Validate the token
        if token != settings.API_SECRET_KEY:
            return Response({"error": "Invalid authorization token."}, status=status.HTTP_403_FORBIDDEN)

        connection_user_id = request.data.get("connection_user_id")
        if not connection_user_id:
            return Response({"error": "Invalid payload."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            instance = ConnectionUser.objects.get(id=int(connection_user_id))

            connection = instance.connection
            user = instance.user
            user_company = user.company
            other_company_id = connection.initiator_id if connection.target_id == user_company.sharemundo_links_id else connection.target_id

            other_company = CompanyProfile.objects.get(sharemundo_links_id=other_company_id)

            projects_to_remove_user = Project.objects.filter(
                user__company__isnull=False
            ).filter(user__company=other_company)

            # Remove the user from team members of these projects
            team_member_objects = ProjectTeamMembers.objects.filter(
                project__in=projects_to_remove_user,
                team_member=user
            )

            for team_member in team_member_objects:
                action_items = ActionItem.objects.filter(
                    assignees=team_member.team_member
                ).distinct()
                manager_tm = ProjectTeamMembers.objects.filter(project=team_member.project, team_member=team_member.project.user).first()
                if not manager_tm:
                    manager_tm, _ = ProjectTeamMembers.objects.get_or_create(
                        project=team_member.project,
                        team_member=team_member.project.user,
                        defaults={'role': 'project manager', 'project_access': "RW"}
                    )
                for action_item in action_items:
                    action_item.assignees.set([manager_tm.team_member])
            
            for project in projects_to_remove_user:
                project_times = ProjectTime.objects.filter(project=project, user=user)
                if project_times.exists():
                    project_times.update(user=project.user)
            team_member_objects.delete()

            return Response({"message": "Projects updated successfully."}, status=status.HTTP_200_OK)
        except ConnectionUser.DoesNotExist:
            return Response({"error": "Connection User not found."}, status=status.HTTP_404_NOT_FOUND)
        except CompanyProfile.DoesNotExist:
            return Response({"error": "CompanyProfile not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RemoveRelatedObjectsOnUserDelete(APIView):
    permission_classes = []

    def post(self, request, *args, **kwargs):
        print("Remove related object called")
        token = request.data.get("SECRET_TOKEN")
        # Check if the token is missing
        if not token:
            return Response({"error": "Authorization token is required."}, status=status.HTTP_401_UNAUTHORIZED)

        # Validate the token
        if token != settings.API_SECRET_KEY:
            return Response({"error": "Invalid authorization token."}, status=status.HTTP_403_FORBIDDEN)
        try:
            user = User.objects.get(id=int(request.data.get("user_id")))
            with transaction.atomic():
                contacts_count = user.contacts.count()
                user.contacts.all().delete()
                if hasattr(user, "azure_tokens"):
                    user.azure_tokens.delete()
                user.oauth_states.all().delete()
                if user.company and user.company.admin_user != user:
                    for action_item in ActionItem.objects.filter(
                        assignees=user
                    ).distinct():
                        action_item.assignees.set([user.company.admin_user])
                    user.authored_documents.update(author=user.company.admin_user)
                    user_projects_count = user.user_projects.count()
                    for project in user.user_projects.all():
                        project.user = user.company.admin_user
                        project.save()
                        if project.team_members.filter(team_member=user.company.admin_user).exists():
                            admin_tm = project.team_members.filter(team_member=user.company.admin_user).first()
                            admin_tm.role = "project manager"
                            admin_tm.project_access = "RW"
                            admin_tm.save()
                            ProjectTeamMembers.objects.filter(
                                project=project,
                                team_member=user,
                                role="project manager",
                                project_access="RW",
                            ).delete()
                            
                        else:
                            ProjectTeamMembers.objects.create(
                                project=project,
                                team_member=user.company.admin_user,
                                role="project manager",
                                project_access="RW",
                            )
                            manager_tm = ProjectTeamMembers.objects.filter(
                                project=project,
                                team_member=user,
                                role="project manager",
                                project_access="RW",
                            ).first()
                            if manager_tm:
                                manager_tm.role = "architect"
                                manager_tm.save()
                                manager_tm.delete()
                        
                else:
                    user_projects_count = user.user_projects.count()
                    user.user_projects.all().delete()
                project_teams_count = user.project_teams.count()
                user.project_teams.all().delete()
                time_expenses = user.time_expenses.all()
                time_expense_count = 0
                for time_expense in time_expenses:
                    if time_expense.project.user != user:
                        time_expense.user = time_expense.project.user
                        time_expense.save()
                        time_expense_count += 1
                if user.company and not user.is_company_admin:
                    company_documents = user.authored_documents.filter(folder__company_profile__isnull=False)
                    company_documents.update(author=user.company.admin_user)
                if hasattr(user, "company_profile"):
                    print(f"{user.company_profile.company_projects.count()} company project has been removed.")
                    user.company_profile.company_projects.all().delete()

                msg = "Successfully removed all associated objects of user being deleted"
                print(f"{msg}:\n{contacts_count} contacts, {user_projects_count} Projects, {project_teams_count} Team member objects.")
            return Response({"message": msg}, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CreateRelatedObjectsOnFreelancerCreate(APIView):
    permission_classes = []

    def post(self, request, *args, **kwargs):
        print("Remove related object called")
        token = request.data.get("SECRET_TOKEN")
        # Check if the token is missing
        if not token:
            return Response({"error": "Authorization token is required."}, status=status.HTTP_401_UNAUTHORIZED)

        # Validate the token
        if token != settings.API_SECRET_KEY:
            return Response({"error": "Invalid authorization token."}, status=status.HTTP_403_FORBIDDEN)
        try:
            user = User.objects.get(id=int(request.data.get("user_id")))
            root_folder = Folder.objects.create(
                freelancer=user,
                name=f"{user.id}_freelancer_root_folder",
                parent=None, 
            )
            predefined_folders = [
                "project information",
                "client correspondence",
                "other correspondence",
                "meeting records",
                "invoices"
            ]
            # Create predefined folders under the root folder
            for folder_name in predefined_folders:
                Folder.objects.create(
                    freelancer=user,
                    name=folder_name,
                    parent=root_folder,
                )
            # Create ChatRoom for the freelancer
            ChatRoom.objects.create(name=user.full_name, freelancer=user, is_direct=False)
            return Response({"message": "Related objects created successfully."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CreateRelatedObjectsOnCompanyCreate(APIView):
    permission_classes = []

    def post(self, request, *args, **kwargs):
        print("Create related object called")
        token = request.data.get("SECRET_TOKEN")
        # Check if the token is missing
        if not token:
            return Response({"error": "Authorization token is required."}, status=status.HTTP_401_UNAUTHORIZED)

        # Validate the token
        if token != settings.API_SECRET_KEY:
            return Response({"error": "Invalid authorization token."}, status=status.HTTP_403_FORBIDDEN)
        try:
            with transaction.atomic():
                company = CompanyProfile.objects.select_for_update().get(id=int(request.data.get("company_id")))
                root_folder, _ = Folder.objects.get_or_create(
                    company_profile=company,
                    name=f"{company.id}_company_root_folder",
                    parent=None,
                )
                predefined_folders = [
                    "project information",
                    "client correspondence",
                    "other correspondence",
                    "meeting records",
                    "invoices"
                ]
                # Create predefined folders under the root folder
                for folder_name in predefined_folders:
                    Folder.objects.get_or_create(
                        company_profile=company,
                        name=folder_name,
                        parent=root_folder,
                    )
                # Create ChatRoom for the company team
                if not ChatRoom.objects.filter(company=company, is_direct=False).exists():
                    ChatRoom.objects.create(
                        company=company, is_direct=False, name=company.company_name,
                    )

                # Update company admin staff profile
                admin_staff_profile = Staff.objects.filter(user=company.admin_user).first()
                if admin_staff_profile is not None:
                    admin_staff_profile.supervisor = admin_staff_profile
                    admin_staff_profile.save(update_fields=["supervisor"])
            
            return Response({"message": "Related objects created successfully."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CreateRelatedObjectsOnUserCreate(APIView):
    permission_classes = []

    def post(self, request, *args, **kwargs):
        print("CreateRelatedObjectsOnUserCreate called")
        token = request.data.get("SECRET_TOKEN")
        # Check if the token is missing
        if not token:
            return Response({"error": "Authorization token is required."}, status=status.HTTP_401_UNAUTHORIZED)

        # Validate the token
        if token != settings.API_SECRET_KEY:
            return Response({"error": "Invalid authorization token."}, status=status.HTTP_403_FORBIDDEN)
        try:
            user = User.objects.get(id=int(request.data.get("user_id")))

            if user.company is not None:
                # Create message read records for the user for comapny's chat room.
                chat_rooms = user.company.company_chats.all()
                message_reads = []
                for chat_room in chat_rooms:
                    for message in chat_room.messages.all():
                        message_reads.append(MessageRead(
                            user=user,
                            message=message,
                        ))         
                MessageRead.objects.bulk_create(message_reads)
                

            return Response({"message": "Related objects created successfully."}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": repr(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UpdateTeamMembersOnRoleChange(APIView):
    """
    API view to handle team member updates when a project role is reassigned.
    This bypasses default JWT validation and manually validates the token.
    """
    permission_classes = []

    def post(self, request, *args, **kwargs):
        token = request.data.get("SECRET_TOKEN")
        # Check if the token is missing
        if not token:
            return Response({"error": "Authorization token is required."}, status=status.HTTP_401_UNAUTHORIZED)

        # Validate the token
        if token != settings.API_SECRET_KEY:
            return Response({"error": "Invalid authorization token."}, status=status.HTTP_403_FORBIDDEN)

        old_role_id = request.data.get("old_role_id")
        new_role_id = request.data.get("new_role_id")
        
        if not old_role_id or not new_role_id:
            return Response({"error": "Both old_role_id and new_role_id are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            old_role = Role.objects.get(id=int(old_role_id))
            new_role = Role.objects.get(id=int(new_role_id))
            
            # Find all team members that have the old role assigned
            team_members = ProjectTeamMembers.objects.filter(project_role=old_role)
            
            updated_count = 0
            for team_member in team_members:
                # Update the team member's role to the new role
                team_member.project_role = new_role
                team_member.role = new_role.name  # Update the role name field as well
                team_member.save(update_fields=['project_role', 'role'])
                updated_count += 1
            
            # Also update any UserRole objects that reference the old role
            user_roles = UserRole.objects.filter(role=old_role)
            user_roles_updated = user_roles.count()
            
            return Response({
                "message": "Team members updated successfully.",
                "team_members_updated": updated_count,
                "user_roles_updated": user_roles_updated,
                "old_role": {
                    "id": old_role.id,
                    "name": old_role.name,
                    "scope": old_role.scope,
                    "type": old_role.type
                },
                "new_role": {
                    "id": new_role.id,
                    "name": new_role.name,
                    "scope": new_role.scope,
                    "type": new_role.type
                }
            }, status=status.HTTP_200_OK)
            
        except Role.DoesNotExist as e:
            return Response({"error": f"Role not found: {str(e)}"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
