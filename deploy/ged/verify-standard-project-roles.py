"""Read-only verification of the existing role-list endpoint after restoration."""
import json
import os
from pathlib import Path
from models.models import CompanyProfile, Role
from rest_framework.test import APIRequestFactory, force_authenticate
from companyprofileapis.views.role import RoleViewSet

company = CompanyProfile.objects.get(pk=1)
specs = json.loads(Path(os.environ['GED_ROLE_SPEC']).read_text(encoding='utf-8-sig'))
expected = {s['name']: set(s['permissions']) for s in specs if s['name'] != 'Project Owner'}
request = APIRequestFactory().get('/api/company/role', {'type':'solution', 'scope':'project', 'page_size':1000})
force_authenticate(request, user=company.admin_user)
response = RoleViewSet.as_view({'get':'list'})(request)
assert response.status_code == 200, response.status_code
rows = response.data['results'] if isinstance(response.data, dict) else response.data
visible = {r['name'] for r in rows}
assert expected.keys() <= visible, (expected.keys(), visible)
roles = []
for name, permissions in expected.items():
    role = Role.objects.get(company=company, name=name, scope='project', type='solution')
    assert role.is_active
    assert set(role.role_permissions.values_list('permission__key', flat=True)) == permissions
    assert any(r['id'] == role.pk for r in rows)
    roles.append({'name':name, 'id':role.pk, 'permissions_exact':True, 'endpoint_visible':True})
assert all(Role.objects.get(pk=r['id']).company_id == company.pk for r in rows)
print(json.dumps({'status':'PASS', 'endpoint_status':response.status_code, 'roles':roles}, ensure_ascii=True, indent=2))
