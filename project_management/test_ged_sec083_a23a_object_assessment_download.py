"""GED-SEC.08.2-A2.3a: the authenticated download of the stored file of an Object Assessment.

Synthetic files in a temporary MEDIA_ROOT and the temporary test database. Requests go through the real view class with
force_authenticate (the authentication middleware itself is NOT traversed here; the local HTTP check is). Only the
licence SQL (tables owned by the admin server, absent from the test database) is stubbed."""
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from ecpmpapp.custom_exceptions import InactiveLicenseException
from models.models import Permission, Role, RolePermission, UserRole
from project_management.models import ObjectAssessment, ProjectTeamMembers
from project_management.test_transmittal import TransmittalTestBase, make_user
from project_management.views.object_assessment import ObjectAssessmentViewSet

DOWNLOAD = ObjectAssessmentViewSet.as_view({"get": "download"})
LICENCE = "ecpmpapp.permissions.LicensePermission.has_permission"
VIEW, FULL, ASSIGNED = ("object_assessments.view", "object_assessments.full_access",
                        "object_assessments.view_assigned_created")


class DownloadBase(TransmittalTestBase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.addCleanup(shutil.rmtree, self._media, True)
        self.addCleanup(self._override.disable)
        licence = patch(LICENCE, return_value=True)
        licence.start()
        self.addCleanup(licence.stop)
        super().setUp()
        self.reader = self.grant("oa.reader@links-local.invalid", self.project, [VIEW])
        self.assessment = self.make_assessment("report.pdf", b"%PDF-1.4 synthetic")

    # --- fixtures ----------------------------------------------------------------------------------------------
    def grant(self, email, project, keys, user=None):
        user = user or make_user(email)
        user.company = project.company_profile
        user.save(update_fields=["company"])
        role = Role.objects.create(name=f"OA role {user.id}-{project.pk}", company=user.company, type="solution",
                                   scope="project", is_default=False, is_active=True, created_by=user)
        for key in keys:
            perm, _ = Permission.objects.get_or_create(
                key=key, defaults={"name": key, "module": key.split(".")[0], "description": "test"})
            RolePermission.objects.get_or_create(role=role, permission=perm)
        ProjectTeamMembers.objects.update_or_create(
            project=project, team_member=user, defaults={"role": "Member", "project_role": role, "project_access": "RW"})
        return user

    def make_assessment(self, name, content, project=None, record_type="Documents", display_name=None, **extra):
        stored = default_storage.save(f"object_assessment/p/{record_type}/{name.replace('/', '_')}", ContentFile(content))
        return ObjectAssessment.objects.create(
            project=project or self.project, record_type=record_type, record_title=f"title {name}",
            file=stored, file_name=display_name if display_name is not None else name, **extra)

    def get(self, user, object_id, method="get"):
        request = getattr(APIRequestFactory(), method)("/x/")
        if user is not None:
            force_authenticate(request, user=user)
        response = DOWNLOAD(request, pk=str(object_id))
        if hasattr(response, "render"):
            response.render()
        return response

    def body(self, response):
        return b"".join(response.streaming_content) if getattr(response, "streaming", False) else response.content


class RouteTests(DownloadBase):
    def test_the_router_exposes_the_exact_path_for_get_and_head(self):
        from django.urls import resolve, reverse
        url = f"/api/project_management/object_assessment/{self.assessment.pk}/download"
        self.assertEqual(resolve(url).func.actions, {"get": "download"})  # HEAD is added by DRF at dispatch (tested below)
        self.assertEqual(reverse("objectassessment-download", args=[self.assessment.pk]).rstrip("/"), url)


class AuthenticationAndLicenceTests(DownloadBase):
    def test_an_anonymous_request_is_a_401(self):
        self.assertEqual(self.get(None, self.assessment.pk).status_code, 401)

    def test_an_invalid_licence_is_refused_with_the_existing_licence_contract_before_any_lookup(self):
        with patch(LICENCE, side_effect=InactiveLicenseException()):
            response = self.get(self.reader, self.assessment.pk)
        self.assertEqual(response.status_code, 423)
        with patch(LICENCE, side_effect=InactiveLicenseException()):
            # even an unknown id gets the licence answer, never a 404 that would help enumerate
            self.assertEqual(self.get(self.reader, 99999999).status_code, 423)


class ScopeAndExistenceTests(DownloadBase):
    def test_other_company_other_project_no_right_and_unknown_id_are_the_same_404(self):
        other_company = self.grant("oa.othertenant@links-local.invalid", self.other_tenant_project, [VIEW, FULL])
        other_project = self.grant("oa.otherproject@links-local.invalid", self.other_project, [VIEW, FULL])
        no_right = self.grant("oa.noright@links-local.invalid", self.project, ["documents.view", "object_assessments.update"])
        outsider = make_user("oa.outsider@links-local.invalid")  # not on the project at all
        responses = [self.get(user, self.assessment.pk) for user in (other_company, other_project, no_right, outsider)]
        unknown = [self.get(user, 99999999) for user in (other_company, other_project, no_right, outsider, self.reader)]
        for response in responses + unknown:
            self.assertEqual(response.status_code, 404)
        # the answers are indistinguishable: same status, same body, same headers (no existence oracle)
        reference = (unknown[0].status_code, unknown[0].data, dict(unknown[0].items()))
        for response in responses + unknown:
            self.assertEqual((response.status_code, response.data, dict(response.items())), reference)

    def test_a_non_numeric_id_is_a_404_too(self):
        self.assertEqual(self.get(self.reader, "abc").status_code, 404)
        self.assertEqual(self.get(self.reader, "-1").status_code, 404)

    def test_documents_view_grants_nothing_here(self):
        docs_only = self.grant("oa.docsonly@links-local.invalid", self.project, ["documents.view", "documents.full_access"])
        self.assertEqual(self.get(docs_only, self.assessment.pk).status_code, 404)

    def test_a_refusal_does_not_read_the_stored_file(self):
        outsider = make_user("oa.outsider2@links-local.invalid")
        with patch("project_management.views.object_assessment.stored_file_response") as served:
            self.assertEqual(self.get(outsider, self.assessment.pk).status_code, 404)
        served.assert_not_called()


class RightsTests(DownloadBase):
    def test_view_and_full_access_read_the_file(self):
        full = self.grant("oa.full@links-local.invalid", self.project, [FULL])
        for user in (self.reader, full):
            response = self.get(user, self.assessment.pk)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.body(response), b"%PDF-1.4 synthetic")

    def test_company_level_project_access_reads_it_like_the_existing_read_rule(self):
        # same company as the project + a company-scope role with projects.full_access (existing rule, no project role)
        owner = self.owner
        role = Role.objects.create(name="OA company role", company=self.company, type="solution", scope="company",
                                   is_default=False, is_active=True, created_by=owner)
        perm, _ = Permission.objects.get_or_create(key="projects.full_access", defaults={
            "name": "projects.full_access", "module": "projects", "description": "test"})
        RolePermission.objects.get_or_create(role=role, permission=perm)
        UserRole.objects.get_or_create(user=owner, role=role)
        self.assertEqual(self.get(owner, self.assessment.pk).status_code, 200)

    def test_view_assigned_created_follows_the_author_the_assignee_and_the_creator_only(self):
        author = self.grant("oa.author@links-local.invalid", self.project, [ASSIGNED])
        assignee = self.grant("oa.assignee@links-local.invalid", self.project, [ASSIGNED])
        creator = self.grant("oa.creator@links-local.invalid", self.project, [ASSIGNED])
        stranger = self.grant("oa.stranger@links-local.invalid", self.project, [ASSIGNED])
        mine = self.make_assessment("mine.txt", b"mine", author=author, assignee=assignee, created_by=creator)
        for user in (assignee, creator):
            self.assertEqual(self.get(user, mine.pk).status_code, 200, user.email)
        # the serializer keeps `assignee` equal to the author: the model-level right is the assignee/creator one
        self.assertEqual(self.get(stranger, mine.pk).status_code, 404)
        # being only the `author` field is not a right of its own in the existing rule (only assignee / creator are)
        self.assertEqual(self.get(author, mine.pk).status_code, 404)
        # and none of them sees another record of the project
        self.assertEqual(self.get(assignee, self.assessment.pk).status_code, 404)

    def test_the_author_is_the_assignee_for_records_made_through_the_api(self):
        # `ObjectAssessmentSerializer` copies the author into `assignee`: so the author reads their own record
        author = self.grant("oa.author2@links-local.invalid", self.project, [ASSIGNED])
        mine = self.make_assessment("api.txt", b"api", author=author, assignee=author, created_by=self.owner)
        self.assertEqual(self.get(author, mine.pk).status_code, 200)

    def test_a_record_of_another_project_is_not_readable_with_a_right_held_elsewhere(self):
        holder = self.grant("oa.holder@links-local.invalid", self.other_project, [FULL, VIEW, ASSIGNED])
        self.assertEqual(self.get(holder, self.assessment.pk).status_code, 404)


class FileTypeTests(DownloadBase):
    def check(self, name, content, mime):
        assessment = self.make_assessment(name, content)
        response = self.get(self.reader, assessment.pk)
        self.assertEqual(response.status_code, 200, name)
        self.assertEqual(self.body(response), content)
        self.assertEqual(response["Content-Type"].split(";")[0], mime)
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertIn(f'filename="{name}"', response["Content-Disposition"])

    def test_pdf_png_txt_csv_and_audio(self):
        self.check("plan.pdf", b"%PDF-1.4 synthetic", "application/pdf")
        self.check("photo.png", b"\x89PNG\r\n\x1a\nsynthetic", "image/png")
        self.check("notes.txt", b"synthetic notes", "text/plain")
        self.check("data.csv", b"a,b\n1,2\n", "text/csv")
        self.check("voice.mp3", b"ID3synthetic", "audio/mpeg")

    def test_a_note_without_file_a_missing_stored_file_and_an_empty_name_are_404_not_500(self):
        note = ObjectAssessment.objects.create(project=self.project, record_type="Notes", record_title="n",
                                               notes_content_html="<p>rich</p>", file_name="n")
        self.assertEqual(self.get(self.reader, note.pk).status_code, 404)
        default_storage.delete(self.assessment.file.name)
        self.assertEqual(self.get(self.reader, self.assessment.pk).status_code, 404)

    def test_special_characters_and_path_separators_in_the_display_name_stay_a_plain_name(self):
        special = self.make_assessment("a.txt", b"x", display_name='re"port é;ü #1.txt')
        response = self.get(self.reader, special.pk)
        self.assertEqual(response.status_code, 200)
        disposition = response["Content-Disposition"]
        self.assertTrue(disposition.startswith("attachment"))
        self.assertNotIn("\n", disposition)
        self.assertNotIn("\r", disposition)
        sneaky = self.make_assessment("b.txt", b"x", display_name="../../etc/passwd.txt")
        response = self.get(self.reader, sneaky.pk)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("..", response["Content-Disposition"])
        self.assertNotIn("/", response["Content-Disposition"])
        backslash = self.make_assessment("c.txt", b"x", display_name="..\\..\\win.txt")
        response = self.get(self.reader, backslash.pk)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("/", response["Content-Disposition"])

    def test_the_stored_name_is_the_fallback_when_there_is_no_display_name(self):
        unnamed = self.make_assessment("stored.txt", b"x", display_name="")
        response = self.get(self.reader, unnamed.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="stored', response["Content-Disposition"])


class ResponseSecurityTests(DownloadBase):
    def test_headers_get_and_head(self):
        for method in ("get", "head"):
            response = self.get(self.reader, self.assessment.pk, method=method)
            self.assertEqual(response.status_code, 200, method)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response["Vary"], "Authorization")
            self.assertEqual(response["Content-Type"].split(";")[0], "application/pdf")
            self.assertTrue(response["Content-Disposition"].startswith("attachment"))
            self.assertEqual(response["Content-Length"], str(len(b"%PDF-1.4 synthetic")))
            self.assertNotIn("Access-Control-Allow-Origin", response)
        head = self.get(self.reader, self.assessment.pk, method="head")
        self.assertTrue(head.streaming)  # a stream, not a body loaded in memory

    def test_nothing_reveals_the_physical_path_and_the_body_is_streamed(self):
        response = self.get(self.reader, self.assessment.pk)
        self.assertTrue(response.streaming)
        self.body(response)
        blob = " ".join(f"{k}:{v}" for k, v in response.items())
        self.assertNotIn("object_assessment", blob)
        self.assertNotIn(self._media, blob)

    def test_a_cross_tenant_reader_never_receives_the_bytes(self):
        other = self.grant("oa.cross@links-local.invalid", self.other_tenant_project, [VIEW, FULL])
        response = self.get(other, self.assessment.pk)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"%PDF", self.body(response))


class PerformanceTests(DownloadBase):
    def test_the_resolution_costs_a_constant_number_of_queries(self):
        def count(assessment):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.get(self.reader, assessment.pk).status_code, 200)
            return len(queries)

        first = count(self.assessment)
        for index in range(5):
            self.make_assessment(f"extra{index}.txt", b"x")
        again = count(self.assessment)
        self.assertEqual(first, again)
        self.assertLessEqual(first, 12)

    def test_the_file_is_not_opened_before_the_right_is_checked_and_is_never_read_into_memory(self):
        outsider = make_user("oa.outsider3@links-local.invalid")
        with patch("django.db.models.fields.files.FieldFile.open") as opened:
            self.get(outsider, self.assessment.pk)
        opened.assert_not_called()
        with patch("project_management.views.document_revision.FileResponse", wraps=__import__(
                "django.http", fromlist=["FileResponse"]).FileResponse) as file_response:
            self.get(self.reader, self.assessment.pk)
        self.assertEqual(file_response.call_count, 1)
        args, kwargs = file_response.call_args
        self.assertTrue(kwargs.get("as_attachment"))
        self.assertTrue(hasattr(args[0], "read"))  # an open file object handed to the stream, not bytes
        self.assertNotIsInstance(args[0], (bytes, bytearray))


class ExistingBehaviourIsUntouchedTests(DownloadBase):
    """The download is an additional action: the verbs and the 403/404 behaviour of the existing endpoints stay as they were
    (the existence oracle of `get_object` is SEC-09's, not this lot's)."""

    def call(self, user, actions, method="get", data=None, **kwargs):
        factory = APIRequestFactory()
        request = factory.get("/x/", data or {}) if method == "get" else getattr(factory, method)("/x/", data, format="json")
        force_authenticate(request, user=user)
        response = ObjectAssessmentViewSet.as_view(actions)(request, **kwargs)
        if hasattr(response, "render"):
            response.render()
        return response

    def test_detail_still_answers_200_to_a_reader_and_still_403_to_an_outsider_with_a_known_id(self):
        self.assertEqual(self.call(self.reader, {"get": "retrieve"}, pk=self.assessment.pk).status_code, 200)
        outsider = self.grant("oa.reg.outsider@links-local.invalid", self.other_tenant_project, [VIEW])
        self.assertEqual(self.call(outsider, {"get": "retrieve"}, pk=self.assessment.pk).status_code, 403)

    def test_the_detail_payload_still_carries_the_file_field_and_no_download_url_was_added(self):
        data = self.call(self.reader, {"get": "retrieve"}, pk=self.assessment.pk).data
        self.assertIn("file", data)
        self.assertFalse([key for key in data if "download" in key])

    def test_list_and_update_still_work(self):
        listed = self.call(self.reader, {"get": "list"}, data={"project": self.project.pk})
        self.assertEqual(listed.status_code, 200)
        self.assertIn(self.assessment.pk, [row["id"] for row in listed.data["results"]])
        writer = self.grant("oa.reg.writer@links-local.invalid", self.project, [VIEW, "object_assessments.update"])
        patched = self.call(writer, {"patch": "partial_update"}, "patch", {"description": "edited"}, pk=self.assessment.pk)
        self.assertEqual(patched.status_code, 200, getattr(patched, "data", None))
        self.assertEqual(ObjectAssessment.objects.get(pk=self.assessment.pk).description, "edited")
        # a reader without the update right is still refused with the existing 403
        self.assertEqual(
            self.call(self.reader, {"patch": "partial_update"}, "patch", {"description": "x"}, pk=self.assessment.pk).status_code, 403)

    def test_the_bulk_update_route_is_still_registered_beside_download(self):
        from django.urls import resolve
        self.assertEqual(resolve("/api/project_management/object_assessment/bulk-update").func.actions, {"patch": "bulk_update"})
