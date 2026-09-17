from django.core.management.base import BaseCommand
from django.db import transaction
from models.models import Role, UserRole
from models.models import CompanyProfile
from project_management.models import Project, ProjectTeamMembers


class Command(BaseCommand):
    help = "Assign PROJECT_OWNER role to project creators and PROJECT_USER role to all project team members"

    def add_arguments(self, parser):
        parser.add_argument(
            '--project-id',
            type=int,
            help='Specific project ID to process (optional)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes',
        )

    def handle(self, *args, **options):
        project_id = options.get('project_id')
        dry_run = options.get('dry_run')
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be made"))
        
        with transaction.atomic():
            if project_id:
                # Process specific project
                self.process_project(project_id, dry_run)
            else:
                # Process all projects
                self.process_all_projects(dry_run)

    def process_all_projects(self, dry_run=False):
        """Process all projects"""
        
        projects = Project.objects.all()
        total_projects = projects.count()
        
        self.stdout.write(f"Processing {total_projects} projects...")
        
        assigned_count = 0
        skipped_count = 0
        error_count = 0
        company_assigned_count = 0
        project_user_roles_assigned = 0
        
        for i, project in enumerate(projects, 1):
            self.stdout.write(f"[{i}/{total_projects}] Processing project: {project.id} - {project.project_name}")
            
            try:
                result = self.process_project(project.id, dry_run)
                if result:
                    assigned_count += result.get('assigned', 0)
                    skipped_count += result.get('skipped', 0)
                    error_count += result.get('error', 0)
                    company_assigned_count += result.get('company_assigned', 0)
                    project_user_roles_assigned += result.get('project_user_roles_assigned', 0)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  - Project {project.id}: Error - {str(e)}"))
                error_count += 1
            
        self.stdout.write(self.style.SUCCESS("Completed processing all projects"))
        self.stdout.write(f"\nOverall Summary:")
        self.stdout.write(f"  - Projects processed: {total_projects}")
        self.stdout.write(f"  - PROJECT_OWNER roles assigned: {assigned_count}")
        self.stdout.write(f"  - PROJECT_USER roles assigned: {project_user_roles_assigned}")
        self.stdout.write(f"  - Company profiles assigned: {company_assigned_count}")
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

        # Check if project has a company profile
        company = project.company_profile
        company_assigned = False
        
        if not company:
            # Get the project user's company profile
            project_user = project.user
            if project_user and hasattr(project_user, 'company') and project_user.company:
                company = project_user.company
                if not dry_run:
                    project.company_profile = company
                    project.save()
                    self.stdout.write(f"  - Assigned company profile {company.company_name} to project {project.id}")
                else:
                    self.stdout.write(f"  - Would assign company profile {company.company_name} to project {project.id}")
                company_assigned = True
            else:
                self.stdout.write(f"  - Project {project.id}: No company profile found and project user has no company, skipping")
                return {'assigned': 0, 'skipped': 1, 'error': 0, 'company_assigned': 0, 'project_user_roles_assigned': 0}

        # Get the PROJECT_OWNER role for this company
        try:
            project_owner_role = Role.objects.get(name="Project Owner", company=company)
        except Role.DoesNotExist:
            self.stdout.write(self.style.WARNING(f"PROJECT_OWNER role not found for company: {company.id}"))
            return {'assigned': 0, 'skipped': 1, 'error': 0, 'company_assigned': company_assigned, 'project_user_roles_assigned': 0}

        # Get the PROJECT_USER role for this company
        try:
            project_user_role = Role.objects.get(name="Project User – Internal", company=company)
        except Role.DoesNotExist:
            self.stdout.write(self.style.WARNING(f"PROJECT_USER role not found for company: {company.id}"))
            return {'assigned': 0, 'skipped': 1, 'error': 0, 'company_assigned': company_assigned, 'project_user_roles_assigned': 0}

        project_creator = project.user
        project_user_roles_assigned = 0
        
        if not project_creator:
            self.stdout.write(f"  - Project {project.id}: No creator found, skipping")
            return {'assigned': 0, 'skipped': 1, 'error': 0, 'company_assigned': company_assigned, 'project_user_roles_assigned': 0}

        # Assign PROJECT_OWNER role to project creator
        existing_user_role = UserRole.objects.filter(
            user=project_creator,
            role=project_owner_role,
            company=company
        ).first()

        if existing_user_role:
            existing_user_role.delete()

        if dry_run:
            self.stdout.write(f"  - Project {project.id}: Would assign PROJECT_OWNER role to {project_creator.full_name}")
        else:
            UserRole.objects.create(
                user=project_creator,
                role=project_owner_role,
                company=company,
                assigned_by=company.admin_user if company.admin_user else None
            )
            # Also update the corresponding Team Member Object
            try:
                team_member = ProjectTeamMembers.objects.get(team_member=project_creator, project=project)
                team_member.project_role = project_owner_role
                team_member.save()
            except ProjectTeamMembers.DoesNotExist:
                self.stdout.write(f"  - Project {project.id}: Warning - No team member record found for {project_creator.full_name}")
            
            self.stdout.write(f"  - Project {project.id}: Assigned PROJECT_OWNER role to {project_creator.full_name}")

        # Assign PROJECT_USER role to all project team members (except the project creator)
        project_team_members = ProjectTeamMembers.objects.filter(project=project).exclude(team_member=project_creator)
        
        for team_member in project_team_members:
            # Check if UserRole already exists for PROJECT_USER role
            existing_project_user_role = UserRole.objects.filter(
                user=team_member.team_member,
                role=project_user_role,
                company=company
            ).first()

            if existing_project_user_role:
                # Update the existing role assignment
                if not dry_run:
                    existing_project_user_role.assigned_by = company.admin_user if company.admin_user else None
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
                        company=company,
                        assigned_by=company.admin_user if company.admin_user else None
                    )
                    self.stdout.write(f"  - Project {project.id}: Assigned PROJECT_USER role to {team_member.team_member.full_name}")
                
                project_user_roles_assigned += 1

            # Clear any existing project_role and assign PROJECT_USER role
            if team_member.project_role and team_member.project_role != project_user_role:
                if dry_run:
                    self.stdout.write(f"  - Project {project.id}: Would clear existing project_role '{team_member.project_role.name}' for {team_member.team_member.full_name}")
                else:
                    # Delete the old UserRole if it exists
                    old_user_role = UserRole.objects.filter(
                        user=team_member.team_member,
                        role=team_member.project_role,
                        company=company
                    ).first()
                    if old_user_role:
                        old_user_role.delete()
                        self.stdout.write(f"  - Project {project.id}: Deleted old UserRole for {team_member.team_member.full_name}")
                    
                    # Update the project_role field
                    team_member.project_role = project_user_role
                    team_member.save()
                    self.stdout.write(f"  - Project {project.id}: Updated project_role to PROJECT_USER for {team_member.team_member.full_name}")
            elif not team_member.project_role:
                if dry_run:
                    self.stdout.write(f"  - Project {project.id}: Would set project_role to PROJECT_USER for {team_member.team_member.full_name}")
                else:
                    team_member.project_role = project_user_role
                    team_member.save()
                    self.stdout.write(f"  - Project {project.id}: Set project_role to PROJECT_USER for {team_member.team_member.full_name}")

        return {
            'assigned': 1, 
            'skipped': 0, 
            'error': 0, 
            'company_assigned': company_assigned,
            'project_user_roles_assigned': project_user_roles_assigned
        }

    