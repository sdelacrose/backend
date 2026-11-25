"""Restore missing standard project role definitions only; dry-run by default.

Invoke through manage.py shell with GED_ROLE_SPEC and GED_ROLE_APPLY=1 to apply.
Never changes an existing role, permission, membership or assignment.
"""
import json
import os
from pathlib import Path
from django.db import transaction
from models.models import CompanyProfile, Permission, Role, RolePermission, UserRole

specs = json.loads(Path(os.environ['GED_ROLE_SPEC']).read_text(encoding='utf-8-sig'))
company = CompanyProfile.objects.get(pk=int(os.environ.get('GED_ROLE_COMPANY', '1')))
expected = {'Project Owner', 'Project Manager', 'Project User – Internal', 'Project User - External'}
assert {s['name'] for s in specs} == expected
assert all(s['scope'] == 'project' and s['type'] == 'solution' for s in specs)
allowed_modules = {'action_item','documents','calendar_events','project','object_assessments',
                   'solution_demands','requirements','transports','bill_of_material',
                   'project_assembly_catalog','site_reporting','funding','phases_management',
                   'milestone_management','ecpmp_configurations'}
assert all(key.split('.')[0] in allowed_modules for s in specs for key in s['permissions'])
apply = os.environ.get('GED_ROLE_APPLY') == '1'
report = {'company': company.pk, 'apply': apply, 'roles': []}
with transaction.atomic():
    assignments_before = list(UserRole.objects.order_by('pk').values_list('pk','user_id','role_id'))
    from project_management.models import ProjectTeamMembers
    members_before = list(ProjectTeamMembers.objects.order_by('pk').values_list('pk','team_member_id','project_role_id'))
    existing_before = list(RolePermission.objects.order_by('pk').values_list('pk','role_id','permission_id'))
    for spec in specs:
        role = Role.objects.filter(company=company, scope='project', type='solution', name=spec['name']).first()
        keys = set(spec['permissions'])
        found = dict(Permission.objects.filter(key__in=keys).values_list('key','pk'))
        missing = sorted(keys - found.keys())
        report['roles'].append({'name':spec['name'], 'existing': bool(role), 'permission_count':len(keys), 'missing_permissions':missing})
        # Owner stays managed by the existing company/project provisioning path.
        # Only restore the three selectable standard member roles.
        if not role and spec['name'] != 'Project Owner':
            if missing:
                raise RuntimeError('Missing permission definitions: ' + repr(missing))
            if apply:
                role = Role.objects.create(company=company, name=spec['name'], scope='project', type='solution',
                    description=spec['description'], is_default=spec['is_default'], is_active=True, created_by=company.admin_user)
                RolePermission.objects.bulk_create([RolePermission(role=role, permission_id=found[key]) for key in sorted(keys)])
                assert set(role.role_permissions.values_list('permission__key', flat=True)) == keys
    assert assignments_before == list(UserRole.objects.order_by('pk').values_list('pk','user_id','role_id'))
    assert members_before == list(ProjectTeamMembers.objects.order_by('pk').values_list('pk','team_member_id','project_role_id'))
    assert existing_before == list(RolePermission.objects.filter(pk__in=[r[0] for r in existing_before]).order_by('pk').values_list('pk','role_id','permission_id'))
print(json.dumps(report, ensure_ascii=True, indent=2))
