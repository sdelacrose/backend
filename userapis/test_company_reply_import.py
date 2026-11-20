from project_management.models import Correspondence
from userapis.test_mailbox_import import ImportBase
from models.models import Role,Permission,RolePermission,UserRole

class CompanyReplyImportTests(ImportBase):
    def grant_company(self):
        role=Role.objects.create(name='Company reply import test',company=self.company,type='solution',scope='company',created_by=self.owner)
        for key in ['documents.correspondence_create','documents.correspondence_view']:
            permission,_=Permission.objects.get_or_create(key=key,defaults={'name':key,'module':'documents','description':'test'})
            RolePermission.objects.create(role=role,permission=permission)
        UserRole.objects.create(user=self.owner,role=role,company=self.company,assigned_by=self.owner)

    def test_company_out_receives_imported_in_even_without_answer_required(self):
        self.grant_company()
        parent=self.make_out(project=None,company_profile=self.company,response_required=False)
        response=self.do(self.owner,self.reference(self.owner,self.graph.add(991)),project=None,parent_correspondence=parent.pk)
        self.assertEqual(response.status_code,201,response.data)
        child=Correspondence.objects.get(pk=response.data['correspondence']['id'])
        self.assertIsNone(child.project_id);self.assertEqual(child.company_profile_id,self.company.pk)
        self.assertEqual((child.direction,child.parent_correspondence_id),('IN',parent.pk))

    def test_omitting_project_without_a_valid_company_parent_is_denied(self):
        self.grant_company()
        response=self.do(self.owner,self.reference(self.owner,self.graph.add(992)),project=None)
        self.assertIn(response.status_code,[400,403])
