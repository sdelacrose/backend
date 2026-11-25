"""GED-SEC.1 -- SEC-01 (folder/search isolation) and SEC-07 (anonymous Transmittal detail).

Requests go through the real URL conf and the real authentication stack (APIClient). Only the license
SQL (tables owned by the admin service, absent from the isolated test DB) is stubbed, and the unlicensed
case exercises the real LicensePermission code path with an empty license query. Isolated test data only.
"""
from unittest.mock import MagicMock, patch

from rest_framework.test import APIClient

from models.models import Permission, Role, RolePermission, UserRole
from project_management.models import Document, Folder, ProjectTeamMembers, Transmittal
from project_management.test_transmittal import (
    TransmittalTestBase,
    grant_transmittal_project_permissions,
    make_user,
)

SEARCH = "/api/project_management/folder/search"
TERM = "zqsec"


def _grant(role, key):
    module = key.split(".")[0]
    permission, _ = Permission.objects.get_or_create(
        key=key, defaults={"name": key, "module": module, "description": "test"}
    )
    RolePermission.objects.get_or_create(role=role, permission=permission)


class FolderSearchSecurityBase(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        self.license_patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        self.license_patcher.start()
        self.addCleanup(self.license_patcher.stop)

        # the Project post_save signal already created each project's root folder
        self.root = self.root_of(self.project)
        self.sub = Folder.objects.create(project=self.project, parent=self.root, name=f"{TERM}-visible-folder")
        self.doc = self._doc(self.sub, f"{TERM}-visible.txt")
        self.root_doc = self._doc(self.root, f"{TERM}-in-root.txt")

        self.other_root = self.root_of(self.other_project)
        self.other_project_doc = self._doc(self.other_root, f"{TERM}-other-project.txt")

        self.foreign_root = self.root_of(self.other_tenant_project)
        self.foreign_sub = Folder.objects.create(
            project=self.other_tenant_project, parent=self.foreign_root, name=f"{TERM}-foreign-folder"
        )
        self.foreign_doc = self._doc(self.foreign_sub, f"{TERM}-foreign.txt")

        # member: documents.view through a PROJECT role of self.project only
        self.member = make_user("sec1.member@links-local.invalid")
        self.member.company = self.company
        self.member.save(update_fields=["company"])
        role = grant_transmittal_project_permissions(self.member, self.project, self.company, ())
        _grant(role, "documents.view")

        # nomember: same tenant, documents.view at COMPANY scope, but not a member of any project
        self.nomember = make_user("sec1.nomember@links-local.invalid")
        self.nomember.company = self.company
        self.nomember.save(update_fields=["company"])
        company_role = Role.objects.create(
            name="sec1 company view", company=self.company, type="solution", scope="company",
            is_default=False, is_active=True, created_by=self.owner,
        )
        _grant(company_role, "documents.view")
        UserRole.objects.create(user=self.nomember, role=company_role, company=self.company, assigned_by=self.owner)

        # noperm: member of the project, but with NO documents right at all
        self.noperm = make_user("sec1.noperm@links-local.invalid")
        self.noperm.company = self.company
        self.noperm.save(update_fields=["company"])
        grant_transmittal_project_permissions(self.noperm, self.project, self.company, ("view",))

        # outsider: another tenant, documents.view at company scope + member of its own project
        self.outsider = self.other_owner
        outsider_role = Role.objects.create(
            name="sec1 outsider company", company=self.other_company, type="solution", scope="company",
            is_default=False, is_active=True, created_by=self.other_owner,
        )
        _grant(outsider_role, "documents.view")
        UserRole.objects.create(
            user=self.other_owner, role=outsider_role, company=self.other_company, assigned_by=self.other_owner
        )
        outsider_project_role = Role.objects.create(
            name="sec1 outsider project", company=self.other_company, type="solution", scope="project",
            is_default=False, is_active=True, created_by=self.other_owner,
        )
        _grant(outsider_project_role, "documents.view")
        ProjectTeamMembers.objects.update_or_create(
            project=self.other_tenant_project, team_member=self.other_owner,
            defaults={"role": "Editor", "project_role": outsider_project_role, "project_access": "RW"},
        )

    @staticmethod
    def root_of(project):
        return Folder.objects.get(project=project, name=f"{project.pk}_root_folder", parent=None)

    def _doc(self, folder, name):
        return self.make_document(folder=folder, name=name)

    def search(self, user, **params):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        params.setdefault("search_term", TERM)
        return client.get(SEARCH, params)

    @staticmethod
    def collect(node):
        """(folder ids, document ids) found anywhere in the serialized tree."""
        folders, documents = set(), set()

        def walk(item):
            if not isinstance(item, dict):
                return
            if item.get("id") is not None and "subfolders" in item:
                folders.add(item["id"])
            for doc in item.get("documents") or []:
                documents.add(doc["id"])
            subs = item.get("subfolders") or []
            for sub in subs.values() if isinstance(subs, dict) else subs:
                walk(sub)

        walk(node)
        return folders, documents


class SearchFoldersAuthorizedTests(FolderSearchSecurityBase):
    def test_member_finds_folders_and_documents_of_the_project(self):
        response = self.search(self.member, project_id=self.project.pk)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        folders, documents = self.collect(response.data)
        self.assertIn(self.sub.pk, folders)
        self.assertEqual(documents, {self.doc.pk, self.root_doc.pk})

    def test_search_by_folder_name_only(self):
        response = self.search(self.member, project_id=self.project.pk, search_term=f"{TERM}-visible-folder")
        self.assertEqual(response.status_code, 200)
        folders, documents = self.collect(response.data)
        self.assertIn(self.sub.pk, folders)

    def test_search_by_document_reference(self):
        response = self.search(self.member, project_id=self.project.pk, search_term=f"{TERM}-in-root")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.collect(response.data)[1], {self.root_doc.pk})

    def test_no_match_is_an_empty_200(self):
        response = self.search(self.member, project_id=self.project.pk, search_term="nothing-matches-this")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.collect(response.data)[1], set())

    def test_company_search_keeps_working_for_its_own_company(self):
        company_root, _ = Folder.objects.get_or_create(
            company_profile=self.company, name=f"{self.company.pk}_company_root_folder", parent=None
        )
        company_doc = self.make_document(folder=Folder.objects.create(
            company_profile=self.company, parent=company_root, name="sec1-company-sub"
        ), name=f"{TERM}-company.txt")
        response = self.search(self.nomember, company_profile_id=self.company.pk)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(self.collect(response.data)[1], {company_doc.pk})
        # a company search never returns project documents
        self.assertFalse(self.collect(response.data)[1] & {self.doc.pk, self.root_doc.pk})

    def test_company_search_is_refused_for_another_company(self):
        response = self.search(self.outsider, company_profile_id=self.company.pk)
        self.assertEqual(response.status_code, 403)

    def test_freelancer_search_is_refused_for_a_company_user(self):
        response = self.search(self.member, freelancer_id=self.member.pk)
        self.assertEqual(response.status_code, 403)


class SearchFoldersIsolationTests(FolderSearchSecurityBase):
    def assertNoLeak(self, response, *forbidden):
        data = getattr(response, "data", None)
        if response.status_code == 200:
            folders, documents = self.collect(data)
            self.assertFalse(documents & {d.pk for d in forbidden})
        else:
            self.assertNotIn("documents", data if isinstance(data, dict) else {})

    def test_other_tenant_cannot_search_the_project(self):
        response = self.search(self.outsider, project_id=self.project.pk)
        self.assertEqual(response.status_code, 404)
        self.assertNoLeak(response, self.doc, self.root_doc)

    def test_other_tenant_response_body_carries_no_document_data(self):
        response = self.search(self.outsider, project_id=self.project.pk)
        body = str(response.content)
        self.assertNotIn(TERM, body)

    def test_non_member_of_the_project_gets_404_even_with_company_view_right(self):
        response = self.search(self.nomember, project_id=self.project.pk)
        self.assertEqual(response.status_code, 404)
        self.assertNoLeak(response, self.doc, self.root_doc)

    def test_member_cannot_search_a_project_they_do_not_belong_to(self):
        response = self.search(self.member, project_id=self.other_project.pk)
        self.assertEqual(response.status_code, 404)
        self.assertNoLeak(response, self.other_project_doc)

    def test_member_cannot_search_another_tenant_project(self):
        response = self.search(self.member, project_id=self.other_tenant_project.pk)
        self.assertEqual(response.status_code, 404)
        self.assertNoLeak(response, self.foreign_doc)

    def test_member_without_documents_right_is_refused(self):
        response = self.search(self.noperm, project_id=self.project.pk)
        self.assertEqual(response.status_code, 403)
        self.assertNoLeak(response, self.doc, self.root_doc)

    def test_unlicensed_user_is_refused(self):
        from ecpmpapp.custom_exceptions import InactiveLicenseException

        self.license_patcher.stop()  # exercise the real LicensePermission with an empty license query
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = None
        with patch("ecpmpapp.permissions.connection.cursor", return_value=cursor):
            response = self.search(self.member, project_id=self.project.pk)
        self.assertEqual(response.status_code, InactiveLicenseException.status_code)
        self.assertNotIn(TERM, str(response.content))

    def test_anonymous_gets_401(self):
        response = self.search(None, project_id=self.project.pk)
        self.assertEqual(response.status_code, 401)

    def test_missing_project_is_404(self):
        self.assertEqual(self.search(self.member, project_id=99999999).status_code, 404)

    def test_non_numeric_project_id_is_400_not_500(self):
        for value in ("abc", "1.5", "-3", "1;DROP"):
            with self.subTest(value=value):
                self.assertEqual(self.search(self.member, project_id=value).status_code, 400)

    def test_non_numeric_company_or_freelancer_id_is_400(self):
        self.assertEqual(self.search(self.member, company_profile_id="abc").status_code, 400)
        self.assertEqual(self.search(self.member, freelancer_id="abc").status_code, 400)

    def test_empty_or_missing_search_term_is_400(self):
        self.assertEqual(self.search(self.member, project_id=self.project.pk, search_term="").status_code, 400)
        client = APIClient()
        client.force_authenticate(self.member)
        self.assertEqual(client.get(SEARCH, {"project_id": self.project.pk}).status_code, 400)
        self.assertEqual(client.get(SEARCH, {"search_term": TERM}).status_code, 400)

    def test_combined_parameters_do_not_bypass_the_project_check(self):
        # project_id wins: a foreign project cannot be reached by adding a permitted company id
        response = self.search(
            self.nomember, project_id=self.project.pk, company_profile_id=self.company.pk,
        )
        self.assertEqual(response.status_code, 404)
        response = self.search(
            self.member, project_id=self.other_tenant_project.pk, company_profile_id=self.company.pk,
        )
        self.assertEqual(response.status_code, 404)
        response = self.search(
            self.member, project_id=self.other_tenant_project.pk, freelancer_id=self.member.pk,
        )
        self.assertEqual(response.status_code, 404)

    def test_folder_query_parameter_cannot_widen_the_scope(self):
        # `folder__project` feeds the permission context of the document helper: it must not let a
        # company-scope search reach project documents of a project the caller belongs to.
        Folder.objects.get_or_create(
            company_profile=self.company, name=f"{self.company.pk}_company_root_folder", parent=None
        )
        response = self.search(
            self.nomember, company_profile_id=self.company.pk, folder__project=self.project.pk,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.collect(response.data)[1], set())

    def test_user_limited_to_created_documents_cannot_read_the_others_through_search(self):
        # "view_created" never opens the project's folder tree (same rule as the folder list), so the
        # documents the user did not create are not reachable through the search either.
        role = ProjectTeamMembers.objects.get(project=self.project, team_member=self.member).project_role
        RolePermission.objects.filter(role=role, permission__key="documents.view").delete()
        _grant(role, "documents.view_created")
        response = self.search(self.member, project_id=self.project.pk)
        self.assertIn(response.status_code, (403, 404))
        self.assertNotIn(TERM, str(response.content))

    def test_other_tenant_data_never_appears_in_an_authorized_response(self):
        response = self.search(self.member, project_id=self.project.pk)
        self.assertEqual(response.status_code, 200)
        folders, documents = self.collect(response.data)
        self.assertFalse(documents & {self.foreign_doc.pk, self.other_project_doc.pk})
        self.assertFalse(folders & {self.foreign_root.pk, self.foreign_sub.pk, self.other_root.pk})
        self.assertNotIn("foreign", str(response.content))

    def test_queries_start_from_the_scoped_helpers_not_global_querysets(self):
        from project_management.services import document_access
        from project_management.views import document as views

        real_folders = views.FolderViewSet.get_queryset
        calls = {"folders": 0, "documents": []}

        def scoped_folders(view):
            calls["folders"] += 1
            return real_folders(view)

        def scoped_documents(request, **kwargs):
            calls["documents"].append(kwargs.get("project_id"))
            return document_access.get_accessible_document_queryset(request, **kwargs)

        with patch.object(views.FolderViewSet, "get_queryset", scoped_folders),                 patch.object(views, "get_accessible_document_queryset", scoped_documents):
            response = self.search(self.member, project_id=self.project.pk)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(calls["folders"], 1)
        # the document helper is evaluated for the searched project; filtering happens in the database
        self.assertEqual(calls["documents"], [self.project.pk])


class TransmittalAnonymousTests(TransmittalTestBase):
    """SEC-07: through the real stack, an anonymous caller gets 401 -- never a 500."""

    def setUp(self):
        super().setUp()
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.transmittal = Transmittal.objects.create(
            direction="OUT", status="DRAFT", project=self.project, external_company=self.client_company,
            subject="sec7", created_by=self.owner,
        )
        self.reader = make_user("sec7.reader@links-local.invalid")
        self.reader.company = self.company
        self.reader.save(update_fields=["company"])
        grant_transmittal_project_permissions(self.reader, self.project, self.company, ("view",))
        self.stranger = make_user("sec7.stranger@links-local.invalid")
        self.stranger.company = self.company
        self.stranger.save(update_fields=["company"])
        self.base = "/api/project_management/transmittal"

    def get(self, path, user=None):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client.get(f"{self.base}{path}")

    def test_anonymous_detail_is_401(self):
        self.assertEqual(self.get(f"/{self.transmittal.pk}").status_code, 401)

    def test_anonymous_list_events_and_client_return_lines_are_refused_without_500(self):
        for path in ("", f"/{self.transmittal.pk}/events", f"/{self.transmittal.pk}/client-return-lines"):
            with self.subTest(path=path):
                self.assertIn(self.get(path).status_code, (401, 404))
                self.assertNotEqual(self.get(path).status_code, 500)

    def test_anonymous_detail_of_a_missing_transmittal_is_401_not_404_or_500(self):
        self.assertEqual(self.get("/99999999").status_code, 401)

    def test_authorized_reader_gets_the_detail(self):
        self.assertEqual(self.get(f"/{self.transmittal.pk}", self.reader).status_code, 200)

    def test_authenticated_user_without_right_is_still_refused(self):
        self.assertEqual(self.get(f"/{self.transmittal.pk}", self.stranger).status_code, 403)

    def test_other_tenant_is_still_refused(self):
        self.assertIn(self.get(f"/{self.transmittal.pk}", self.other_owner).status_code, (403, 404))

    def test_missing_transmittal_is_404_for_an_authorized_user(self):
        self.assertEqual(self.get("/99999999", self.reader).status_code, 404)

    def test_no_business_permission_or_data_changed(self):
        before = (Transmittal.objects.count(), RolePermission.objects.count())
        self.get(f"/{self.transmittal.pk}")
        self.get(f"/{self.transmittal.pk}", self.reader)
        self.assertEqual(before, (Transmittal.objects.count(), RolePermission.objects.count()))
