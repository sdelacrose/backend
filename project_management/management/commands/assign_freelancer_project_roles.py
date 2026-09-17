from django.core.management.base import BaseCommand
from django.db import transaction
from models.models import Role, UserRole
from project_management.models import Project, ProjectTeamMembers


class Command(BaseCommand):
    help = "Assign PROJECT_OWNER role to freelancer project creators and PROJECT_USER role to all project team members on freelancer-created projects"

    def add_arguments(self, parser):
        parser.add_argument(
            '--project-id',
            type=int,
            help='Specific project ID to process (optional)',
        )
        parser.add_argument(
            '--freelancer-id',
            type=int,
            help='Process all projects created by a specific freelancer (optional)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )

    def handle(self, *args, **options):
        project_id = options.get('project_id')
        freelancer_id = options.get('freelancer_id')
        dry_run = options.get('dry_run')
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be made"))
        
        with transaction.atomic():
            if project_id:
                # Process specific project
                self.process_project(project_id, dry_run)
            elif freelancer_id:
                # Process all projects by specific freelancer
                self.process_freelancer_projects(freelancer_id, dry_run)
            else:
                # Process all freelancer-created projects
                self.process_all_freelancer_projects(dry_run)

    def process_all_freelancer_projects(self, dry_run=False):
        """Process all projects created by freelancers"""
        
        # Get all projects where company_profile is null and project.user is a freelancer
        projects = Project.objects.filter(
            company_profile__isnull=True,
            user__user_type='freelancer'
        )
        total_projects = projects.count()
        
        self.stdout.write(f"Processing {total_projects} freelancer-created projects...")
        
        assigned_count = 0
        skipped_count = 0
        error_count = 0
        project_user_roles_assigned = 0
        
        for i, project in enumerate(projects, 1):
            self.stdout.write(f"[{i}/{total_projects}] Processing project: {project.id} - {project.project_name}")
            
            try:
                result = self.process_project(project.id, dry_run)
                if result:
                    assigned_count += result.get('assigned', 0)
                    skipped_count += result.get('skipped', 0)
                    error_count += result.get('error', 0)
                    project_user_roles_assigned += result.get('project_user_roles_assigned', 0)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  - Project {project.id}: Error - {str(e)}"))
                error_count += 1
            
        self.stdout.write(self.style.SUCCESS("Completed processing all freelancer-created projects"))
        self.stdout.write(f"\nOverall Summary:")
        self.stdout.write(f"  - Projects processed: {total_projects}")
        self.stdout.write(f"  - PROJECT_OWNER roles assigned: {assigned_count}")
        self.stdout.write(f"  - PROJECT_USER roles assigned: {project_user_roles_assigned}")
        self.stdout.write(f"  - Skipped: {skipped_count}")
        self.stdout.write(f"  - Errors: {error_count}")

    def process_freelancer_projects(self, freelancer_id, dry_run=False):
        """Process all projects created by a specific freelancer"""
        
        try:
            from models.models import User
            freelancer = User.objects.get(id=freelancer_id, user_type='freelancer')
        except User.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"Freelancer with ID {freelancer_id} not found"))
            return
        
        # Get all projects where company_profile is null and project.user is this freelancer
        projects = Project.objects.filter(
            company_profile__isnull=True,
            user=freelancer
        )
        total_projects = projects.count()
        
        self.stdout.write(f"Processing {total_projects} projects created by freelancer: {freelancer.full_name} (ID: {freelancer_id})")
        
        assigned_count = 0
        skipped_count = 0
        error_count = 0
        project_user_roles_assigned = 0
        
        for i, project in enumerate(projects, 1):
            self.stdout.write(f"[{i}/{total_projects}] Processing project: {project.id} - {project.project_name}")
            
            try:
                result = self.process_project(project.id, dry_run)
                if result:
                    assigned_count += result.get('assigned', 0)
                    skipped_count += result.get('skipped', 0)
                    error_count += result.get('error', 0)
                    project_user_roles_assigned += result.get('project_user_roles_assigned', 0)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  - Project {project.id}: Error - {str(e)}"))
                error_count += 1
            
        self.stdout.write(self.style.SUCCESS(f"Completed processing projects for freelancer: {freelancer.full_name}"))
        self.stdout.write(f"\nSummary for freelancer {freelancer.full_name}:")
        self.stdout.write(f"  - Projects processed: {total_projects}")
        self.stdout.write(f"  - PROJECT_OWNER roles assigned: {assigned_count}")
        self.stdout.write(f"  - PROJECT_USER roles assigned: {project_user_roles_assigned}")
        self.stdout.write(f"  - Skipped: {skipped_count}")
        self.stdout.write(f"  - Errors: {error_count}")

    def process_project(self, project_id, dry_run=False):
        """Process a specific project"""
        
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"Project with ID {project_id} not found"))
            return None

        self.stdout.write(f"Processing project: {project.id} - {project.project_name}")

        # Verify this is a freelancer-created project
        if project.company_profile:
            self.stdout.write(f"  - Project {project.id}: This is a company project (has company_profile), not a freelancer project. Skipping.")
            return {'assigned': 0, 'skipped': 1, 'error': 0, 'project_user_roles_assigned': 0}

        # Get the project user (freelancer)
        project_user = project.user
        if not project_user:
            self.stdout.write(f"  - Project {project.id}: No project user found, skipping")
            return {'assigned': 0, 'skipped': 1, 'error': 0, 'project_user_roles_assigned': 0}

        # Verify the project user is a freelancer
        if project_user.user_type != 'freelancer':
            self.stdout.write(f"  - Project {project.id}: Project user is not a freelancer ({project_user.user_type}), skipping")
            return {'assigned': 0, 'skipped': 1, 'error': 0, 'project_user_roles_assigned': 0}

        # Get the PROJECT_OWNER role for this freelancer
        try:
            project_owner_role = Role.objects.get(name="Project Owner", freelancer=project_user, company=None)
        except Role.DoesNotExist:
            self.stdout.write(self.style.WARNING(f"PROJECT_OWNER role not found for freelancer: {project_user.id} - {project_user.full_name}"))
            return {'assigned': 0, 'skipped': 1, 'error': 0, 'project_user_roles_assigned': 0}

        # Get the PROJECT_USER role for this freelancer
        try:
            project_user_role = Role.objects.get(name="Project User – Internal", freelancer=project_user, company=None)
        except Role.DoesNotExist:
            self.stdout.write(self.style.WARNING(f"PROJECT_USER role not found for freelancer: {project_user.id} - {project_user.full_name}"))
            return {'assigned': 0, 'skipped': 1, 'error': 0, 'project_user_roles_assigned': 0}

        project_user_roles_assigned = 0
        
        # Assign PROJECT_OWNER role to project user (freelancer)
        existing_user_role = UserRole.objects.filter(
            user=project_user,
            role=project_owner_role,
            company=None,  # Freelancer projects don't have company
            freelancer=project_user  # Set the freelancer field
        ).first()

        if existing_user_role:
            if dry_run:
                self.stdout.write(f"  - Project {project.id}: PROJECT_OWNER role already assigned to {project_user.full_name}")
            else:
                self.stdout.write(f"  - Project {project.id}: PROJECT_OWNER role already assigned to {project_user.full_name}")
            
            # Also update the corresponding Team Member Object even if role already exists
            try:
                team_member = ProjectTeamMembers.objects.get(team_member=project_user, project=project)
                if team_member.project_role != project_owner_role:
                    if dry_run:
                        self.stdout.write(f"  - Project {project.id}: Would update project_role to PROJECT_OWNER for {project_user.full_name}")
                    else:
                        team_member.project_role = project_owner_role
                        team_member.save()
                        self.stdout.write(f"  - Project {project.id}: Updated project_role to PROJECT_OWNER for {project_user.full_name}")
            except ProjectTeamMembers.DoesNotExist:
                self.stdout.write(f"  - Project {project.id}: Warning - No team member record found for {project_user.full_name}")
        else:
            if dry_run:
                self.stdout.write(f"  - Project {project.id}: Would assign PROJECT_OWNER role to {project_user.full_name}")
            else:
                UserRole.objects.create(
                    user=project_user,
                    role=project_owner_role,
                    company=None,  # Freelancer projects don't have company
                    freelancer=project_user,  # Set the freelancer field
                    assigned_by=project_user
                )
                # Also update the corresponding Team Member Object
                try:
                    team_member = ProjectTeamMembers.objects.get(team_member=project_user, project=project)
                    team_member.project_role = project_owner_role
                    team_member.save()
                    self.stdout.write(f"  - Project {project.id}: Updated project_role to PROJECT_OWNER for {project_user.full_name}")
                except ProjectTeamMembers.DoesNotExist:
                    self.stdout.write(f"  - Project {project.id}: Warning - No team member record found for {project_user.full_name}")
                
                self.stdout.write(f"  - Project {project.id}: Assigned PROJECT_OWNER role to {project_user.full_name}")

        # Assign PROJECT_USER role to all project team members (except the project user)
        project_team_members = ProjectTeamMembers.objects.filter(project=project).exclude(team_member=project_user)
        
        for team_member in project_team_members:
            # Check if UserRole already exists for PROJECT_USER role
            existing_project_user_role = UserRole.objects.filter(
                user=team_member.team_member,
                role=project_user_role,
                company=None,  # Freelancer projects don't have company
                freelancer=project_user  # Set the freelancer field
            ).first()

            if existing_project_user_role:
                # Update the existing role assignment
                if not dry_run:
                    existing_project_user_role.assigned_by = project_user
                    existing_project_user_role.save()
                self.stdout.write(f"  - Project {project.id}: Updated PROJECT_USER role for {team_member.team_member.full_name}")
            else:
                # Create new role assignment
                if dry_run:
                    self.stdout.write(f"  - Project {project.id}: Would assign PROJECT_USER role to {team_member.team_member.full_name}")
                else:
                    UserRole.objects.create(
                        user=team_member.team_member,
                        role=project_user_role,
                        company=None,  # Freelancer projects don't have company
                        freelancer=project_user,  # Set the freelancer field
                        assigned_by=project_user
                    )
                    self.stdout.write(f"  - Project {project.id}: Assigned PROJECT_USER role to {team_member.team_member.full_name}")
                
                project_user_roles_assigned += 1

            # Update the project_role field in ProjectTeamMembers
            if team_member.project_role != project_user_role:
                if dry_run:
                    self.stdout.write(f"  - Project {project.id}: Would update project_role to PROJECT_USER for {team_member.team_member.full_name}")
                else:
                    team_member.project_role = project_user_role
                    team_member.save()
                    self.stdout.write(f"  - Project {project.id}: Updated project_role to PROJECT_USER for {team_member.team_member.full_name}")

        return {
            'assigned': 1, 
            'skipped': 0, 
            'error': 0, 
            'project_user_roles_assigned': project_user_roles_assigned
        } 