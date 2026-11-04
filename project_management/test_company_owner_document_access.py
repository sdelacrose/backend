from types import SimpleNamespace
from unittest.mock import patch
from models.models import Permission,RolePermission,UserRole
from project_management.permissions import DocumentPermission,TransmittalPermission,CorrespondencePermission,ECPMPConfigPermission,ActionItemPermission
from project_management.test_transmittal import TransmittalTestBase,grant_transmittal_permissions


class CompanyOwnerDocumentAccessTests(TransmittalTestBase):
    def test_api_owner_outside_team_can_launch_and_act_as_assigned_author_approver(self):
        from django.db import connection
        # The solution reads these admin-owned tables through SQL; their models
        # are not installed in this app. Populate real rows in the isolated test DB.
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO package_license_product (name,description,price,status) VALUES (%s,'test',0,'Active') RETURNING id", ['Energy Consultant Solution'])
            product_id=cursor.fetchone()[0]
            cursor.execute("INSERT INTO package_license_package (is_active,created_at,updated_at,expires_at,status,is_freetrial) VALUES (TRUE,NOW(),NOW(),NOW()+INTERVAL '30 days','Cancelled',TRUE) RETURNING id")
            package_id=cursor.fetchone()[0]
            cursor.execute("INSERT INTO package_license_license (is_active,created_at,updated_at,package_id,product_id,user_id,license_assigned_at) VALUES (TRUE,NOW(),NOW(),%s,%s,%s,NOW())", [package_id,product_id,self.owner.pk])
        from rest_framework.test import APIRequestFactory, force_authenticate
        from project_management.views.document_workflow import DocumentWorkflowViewSet
        from project_management.models import ProjectTeamMembers
        self.grant_company_documents(self.owner)
        self.project.user=self.approver
        self.project.save(update_fields=['user'])
        ProjectTeamMembers.objects.filter(team_member=self.owner).delete()
        document=self.make_document()
        document.author=self.owner
        document.save(update_fields=['author'])
        revision=self.add_revision(document)
        def call(method,action,data=None):
            request=getattr(APIRequestFactory(),method)('/test',data or {},format='json')
            force_authenticate(request,user=self.owner)
            return DocumentWorkflowViewSet.as_view({method:action})(request,document_id=document.pk,revision_id=revision.pk)
        refused=call('post','start',{'workflow_type':'TWO_STEP','author':self.owner.pk,'approver':self.approver.pk})
        self.assertEqual(refused.status_code,400,refused.data)
        self.assertIn('approver',refused.data)
        from project_management.models import DocumentWorkflow
        self.assertFalse(DocumentWorkflow.objects.filter(revision=revision).exists())
        response=call('post','start',{'workflow_type':'TWO_STEP','author':self.owner.pk,'approver':self.owner.pk})
        self.assertEqual(response.status_code,201,response.data)
        self.assertEqual(response.data['status'],'APPROVE')
        self.assertEqual(call('get','retrieve').status_code,200)
        response=call('post','act',{'action':'APPROVE'})
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['status'],'APPROVED')

    def test_api_owner_config_read_does_not_allow_any_configuration_write(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from project_management.views.ecpmp_config import ECPMPConfigViewSet
        from project_management.models import ECPMPConfig, ProjectTeamMembers
        self.grant_company_documents(self.owner)
        ProjectTeamMembers.objects.filter(team_member=self.owner).delete()
        config,_=ECPMPConfig.objects.get_or_create(project=self.project)
        original=config.status_options
        for method,action in [('get','retrieve'),('patch','partial_update'),('put','update'),('delete','destroy'),('post','reset_config'),('post','sync_with_admin')]:
            request=getattr(APIRequestFactory(),method)('/test',{'status_options':['changed']},format='json')
            force_authenticate(request,user=self.owner)
            response=ECPMPConfigViewSet.as_view({method:action})(request,pk=config.pk)
            self.assertEqual(response.status_code,200 if method=='get' else 403,(action,response.data))
        config.refresh_from_db()
        self.assertEqual(config.status_options,original)

    def test_documentary_owner_cannot_sign_for_assigned_actor_or_gain_unknown_right(self):
        from rest_framework.exceptions import PermissionDenied
        from project_management.services.document_workflow import start_workflow, perform_action
        self.grant_company_documents(self.owner)
        for action in ['manage_cycle','transmittal_manage_cycle','correspondence_manage_cycle','sign','approve','unknown']:
            self.assertFalse(self.check(DocumentPermission,self.owner,self.project,action))
        document=self.make_document()
        revision=self.add_revision(document)
        workflow=start_workflow(revision,'TWO_STEP',self.approver,None,self.approver,self.approver)
        with self.assertRaises(PermissionDenied):
            perform_action(workflow,self.owner,'SIGN','')
        perform_action(workflow,self.approver,'SIGN','')
        with self.assertRaises(PermissionDenied):
            perform_action(workflow,self.owner,'APPROVE','')

    def test_api_non_member_owner_document_correspondence_transmittal_paths(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from project_management.views.document import DocumentViewSet
        from project_management.views.correspondence import CorrespondenceViewSet
        from project_management.views.transmittal import TransmittalViewSet
        from project_management.models import ProjectTeamMembers
        self.grant_company_documents(self.owner)
        self.project.user = self.approver
        self.project.save(update_fields=['user'])
        ProjectTeamMembers.objects.filter(team_member=self.owner).delete()
        factory = APIRequestFactory()
        def call(view, method, action, data=None, pk=None):
            request = getattr(factory, method)('/test', data or {}, format='json')
            force_authenticate(request, user=self.owner)
            return view.as_view({method:action})(request, **({'pk':pk} if pk else {}))
        trans = call(TransmittalViewSet, 'post', 'create', {
            'direction':'OUT','status':'DRAFT','project':self.project.pk,
            'external_company':self.client_company.pk,'subject':'Owner test'})
        self.assertEqual(trans.status_code,201,trans.data)
        corr = call(CorrespondenceViewSet, 'post', 'create', {
            'direction':'OUT','project':self.project.pk,'recipient_company':self.client_company.pk,
            'subject':'Owner correspondence'})
        self.assertEqual(corr.status_code,201,corr.data)
        doc = self.make_document()
        foreign_doc = self.make_document(folder=self.other_tenant_folder)
        for view, pk, params, foreign_pk in [
            (DocumentViewSet,doc.pk,{'folder__project':self.project.pk},foreign_doc.pk),
            (CorrespondenceViewSet,corr.data['id'],{'project':self.project.pk},None),
            (TransmittalViewSet,trans.data['id'],{'project':self.project.pk},None),
        ]:
            with self.subTest(view=view.__name__):
                response=call(view,'get','list',params)
                self.assertEqual(response.status_code,200,response.data)
                rows=response.data.get('results',[]) if isinstance(response.data,dict) else response.data
                self.assertIn(pk,[row['id'] for row in rows])
                self.assertEqual(call(view,'get','retrieve',pk=pk).status_code,200)
                data={'file_name':'Owner updated'} if view is DocumentViewSet else {'subject':'Owner updated'}
                response=call(view,'patch','partial_update',data,pk)
                self.assertEqual(response.status_code,200,response.data)
                if foreign_pk:
                    self.assertIn(call(view,'get','retrieve',pk=foreign_pk).status_code,[403,404])
        denied=call(TransmittalViewSet,'post','create',{
            'direction':'OUT','project':self.other_tenant_project.pk,
            'external_company':self.client_company.pk})
        self.assertEqual(denied.status_code,403)

    def test_config_queryset_includes_other_creators_projects_only_in_owner_company(self):
        from project_management.models import ECPMPConfig, ProjectTeamMembers
        from project_management.views.ecpmp_config import ECPMPConfigViewSet
        self.grant_company_documents(self.owner)
        # Membership must not be needed: this reproduces the recipe gap.
        ProjectTeamMembers.objects.filter(team_member=self.owner).delete()
        own_config, _ = ECPMPConfig.objects.get_or_create(project=self.other_project)
        foreign_config, _ = ECPMPConfig.objects.get_or_create(project=self.other_tenant_project)
        view = ECPMPConfigViewSet()
        view.request = SimpleNamespace(user=self.owner, query_params={})
        with patch('project_management.views.ecpmp_config.get_product_name_from_request', return_value=self.other_project.solution):
            ids = set(view.get_queryset().values_list('pk', flat=True))
        self.assertIn(own_config.pk, ids)
        self.assertNotIn(foreign_config.pk, ids)
        view.request.user = self.approver
        with patch('project_management.views.ecpmp_config.get_product_name_from_request', return_value=self.other_project.solution):
            self.assertNotIn(own_config.pk, set(view.get_queryset().values_list('pk', flat=True)))

    def grant_company_documents(self,user):
        grant_transmittal_permissions(user,self.company,actions=())
        role=UserRole.objects.get(user=user,role__company=self.company,role__scope='company').role
        permission,_=Permission.objects.get_or_create(key='documents.full_access',defaults={'module':'documents','name':'Documents full access','description':'test'})
        RolePermission.objects.get_or_create(role=role,permission=permission)
        return role

    def check(self,cls,user,project,action='full_access',module='documents'):
        return cls()._check_permission(user,module,action,'project',{'project':project})

    def test_owner_existing_company_document_rights_cover_every_own_project(self):
        self.grant_company_documents(self.owner)
        for project in [self.project,self.other_project]:
            for cls in [DocumentPermission,CorrespondencePermission,TransmittalPermission]:
                with self.subTest(project=project.pk,permission=cls.__name__):
                    self.assertTrue(self.check(cls,self.owner,project))
        request=SimpleNamespace(user=self.owner,method='POST',data={'project':self.other_project.pk})
        self.assertTrue(TransmittalPermission('create').has_permission(request,SimpleNamespace(action='create')))

    def test_owner_does_not_gain_other_company_project(self):
        self.grant_company_documents(self.owner)
        for cls in [DocumentPermission,CorrespondencePermission,TransmittalPermission]:
            self.assertFalse(self.check(cls,self.owner,self.other_tenant_project))

    def test_non_owner_company_full_access_does_not_gain_project_scope(self):
        self.grant_company_documents(self.approver)
        for cls in [DocumentPermission,CorrespondencePermission,TransmittalPermission]:
            self.assertFalse(self.check(cls,self.approver,self.project))

    def test_owner_without_documentary_entitlement_is_not_automatically_granted(self):
        self.assertFalse(self.check(TransmittalPermission,self.owner,self.project))

    def test_inactive_owner_or_role_is_not_eligible(self):
        role=self.grant_company_documents(self.owner)
        self.owner.is_active=False
        self.assertFalse(self.check(TransmittalPermission,self.owner,self.project))
        self.owner.is_active=True
        role.is_active=False;role.save(update_fields=['is_active'])
        self.assertFalse(self.check(TransmittalPermission,self.owner,self.project))

    def test_no_document_control_or_other_module_or_config_write_granted(self):
        self.grant_company_documents(self.owner)
        self.assertFalse(self.check(TransmittalPermission,self.owner,self.project,'manage_cycle'))
        self.assertFalse(self.check(ActionItemPermission,self.owner,self.project,'full_access','action_item'))
        self.assertTrue(self.check(ECPMPConfigPermission,self.owner,self.project,'view','ecpmp_configurations'))
        for action in ['update','delete','full_access','sync_admin']:
            self.assertFalse(self.check(ECPMPConfigPermission,self.owner,self.project,action,'ecpmp_configurations'))
        self.assertFalse(self.check(ECPMPConfigPermission,self.owner,self.other_tenant_project,'view','ecpmp_configurations'))

    def test_project_id_context_supported_without_creating_memberships_or_roles(self):
        self.grant_company_documents(self.owner)
        from project_management.models import ProjectTeamMembers
        from models.models import Role
        before=(ProjectTeamMembers.objects.count(),Role.objects.count(),RolePermission.objects.count())
        self.assertTrue(self.check(TransmittalPermission,self.owner,self.project.pk))
        self.assertEqual(before,(ProjectTeamMembers.objects.count(),Role.objects.count(),RolePermission.objects.count()))
