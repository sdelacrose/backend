"""GED-WF.4.1: client surveillance codes configuration per project (real project roles, no permission stub)."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import Permission, Role, RolePermission
from project_management.models import (
    ClientIssue, ClientSurveillance, ClientSurveillanceCode, ProjectTeamMembers,
)
from project_management.services.client_surveillance_code import (
    CATEGORIES, category_consequences, list_effective_codes, reset_category, save_codes,
)
from project_management.test_transmittal import TransmittalTestBase, make_project, make_user
from project_management.views.client_surveillance_code import ClientSurveillanceCodeViewSet

URL = "/x/"


class CodesBase(TransmittalTestBase):
    def grant(self, email, project, keys, company=None):
        user = make_user(email)
        user.company = company or self.company
        user.save(update_fields=["company"])
        role = Role.objects.create(
            name=f"WF41 role {user.id}", company=user.company, type="solution", scope="project",
            is_default=False, is_active=True, created_by=user,
        )
        for key in keys:
            perm, _ = Permission.objects.get_or_create(
                key=key, defaults={"name": key, "module": key.split(".")[0], "description": "test"},
            )
            RolePermission.objects.get_or_create(role=role, permission=perm)
        ProjectTeamMembers.objects.update_or_create(
            project=project, team_member=user, defaults={"role": "Member", "project_role": role, "project_access": "RW"},
        )
        return user

    def call(self, method, user, project, category=None, action="list", body=None):
        factory = APIRequestFactory()
        data = {"project": project.pk, **(body or {})}
        if method == "get":
            request = factory.get(URL, {"project": project.pk})
        else:
            request = getattr(factory, method)(URL, data, format="json")
        force_authenticate(request, user=user)
        kwargs = {"category": category} if category else {}
        return ClientSurveillanceCodeViewSet.as_view({method: action}, permission_classes=[])(request, **kwargs)

    def admin(self, project=None):
        return self.grant(f"wf41.admin{(project or self.project).pk}@links-local.invalid", project or self.project,
                          ["ecpmp_configurations.update", "documents.view"])

    def reader(self):
        return self.grant("wf41.reader@links-local.invalid", self.project, ["documents.view"])

    def codes(self, response):
        return {row["category"]: row for row in response.data["codes"]}


class DefaultsAndServiceTests(CodesBase):
    def test_defaults_without_any_row(self):
        rows = list_effective_codes(self.project)
        self.assertEqual([(r["category"], r["code"]) for r in rows],
                         [("ACCEPTED", "A"), ("ACCEPTED_WITH_COMMENTS", "B"), ("COMMENTED", "C"), ("REJECTED", "D")])
        self.assertTrue(all(not r["customized"] and r["label_en"] is None for r in rows))
        self.assertEqual(ClientSurveillanceCode.objects.count(), 0)  # reading creates nothing

    def test_per_project_customisation_and_isolation(self):
        save_codes(self.project, [{"category": "ACCEPTED_WITH_COMMENTS", "code": "B1", "label_fr": "Accepté, avec réserves"}], self.owner)
        mine = {r["category"]: r for r in list_effective_codes(self.project)}
        other = {r["category"]: r for r in list_effective_codes(self.other_project)}
        self.assertEqual(mine["ACCEPTED_WITH_COMMENTS"]["code"], "B1")
        self.assertEqual(mine["ACCEPTED_WITH_COMMENTS"]["label_fr"], "Accepté, avec réserves")
        self.assertTrue(mine["ACCEPTED_WITH_COMMENTS"]["customized"])
        self.assertEqual(other["ACCEPTED_WITH_COMMENTS"]["code"], "B")  # another project keeps its own values
        self.assertEqual(ClientSurveillanceCode.objects.filter(project=self.other_project).count(), 0)
        # the same visible code may exist in two projects
        save_codes(self.other_project, [{"category": "ACCEPTED_WITH_COMMENTS", "code": "B1"}], self.owner)
        self.assertEqual(ClientSurveillanceCode.objects.filter(code="B1").count(), 2)

    def test_code_unique_case_insensitive_including_defaults_of_other_categories(self):
        with self.assertRaises(ValidationError):
            save_codes(self.project, [{"category": "ACCEPTED_WITH_COMMENTS", "code": "c"}], self.owner)  # C is the default of COMMENTED
        save_codes(self.project, [{"category": "ACCEPTED_WITH_COMMENTS", "code": "Bx"}], self.owner)
        with self.assertRaises(ValidationError):
            save_codes(self.project, [{"category": "COMMENTED", "code": "bX"}], self.owner)
        # database level: the unique index is case-insensitive too, whatever the application checks
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientSurveillanceCode.objects.bulk_create(
                [ClientSurveillanceCode(project=self.project, category="REJECTED", code="BX")])

    def test_two_categories_can_swap_their_codes_in_one_save(self):
        save_codes(self.project, [{"category": "ACCEPTED", "code": "B"}, {"category": "ACCEPTED_WITH_COMMENTS", "code": "A"}], self.owner)
        rows = {r["category"]: r["code"] for r in list_effective_codes(self.project)}
        self.assertEqual((rows["ACCEPTED"], rows["ACCEPTED_WITH_COMMENTS"]), ("B", "A"))

    def test_code_format_and_label_length(self):
        for bad in ("", "  ", "ABCDEFGHIJK", "A-1", "é", "A B"):
            with self.assertRaises(ValidationError, msg=bad):
                save_codes(self.project, [{"category": "ACCEPTED", "code": bad}], self.owner)
        with self.assertRaises(ValidationError):
            save_codes(self.project, [{"category": "ACCEPTED", "code": "A", "label_en": "x" * 101}], self.owner)
        with self.assertRaises(ValidationError):
            save_codes(self.project, [{"category": "NOPE", "code": "Z"}], self.owner)
        self.assertEqual(ClientSurveillanceCode.objects.count(), 0)  # a refused save writes nothing

    def test_category_cannot_be_changed_after_creation(self):
        save_codes(self.project, [{"category": "COMMENTED", "code": "C9"}], self.owner)
        row = ClientSurveillanceCode.objects.get()
        row.category = "REJECTED"
        with self.assertRaises(ValidationError):
            row.save()
        self.assertEqual(ClientSurveillanceCode.objects.get().category, "COMMENTED")

    def test_blank_label_goes_back_to_the_default_and_default_values_store_no_row(self):
        save_codes(self.project, [{"category": "REJECTED", "code": "D", "label_de": "Abgelehnt!"}], self.owner)
        self.assertEqual(ClientSurveillanceCode.objects.count(), 1)
        save_codes(self.project, [{"category": "REJECTED", "code": "D", "label_de": "  "}], self.owner)
        self.assertEqual(ClientSurveillanceCode.objects.count(), 0)

    def test_reset_returns_to_defaults_and_refuses_a_taken_default_code(self):
        save_codes(self.project, [{"category": "ACCEPTED_WITH_COMMENTS", "code": "B1", "label_en": "Accepted, with notes"}], self.owner)
        rows = {r["category"]: r for r in reset_category(self.project, "ACCEPTED_WITH_COMMENTS", self.owner)}
        self.assertEqual((rows["ACCEPTED_WITH_COMMENTS"]["code"], rows["ACCEPTED_WITH_COMMENTS"]["label_en"]), ("B", None))
        save_codes(self.project, [{"category": "ACCEPTED", "code": "Z"}, {"category": "COMMENTED", "code": "A"}], self.owner)
        with self.assertRaises(ValidationError):
            reset_category(self.project, "ACCEPTED", self.owner)  # COMMENTED now uses A
        with self.assertRaises(ValidationError):
            reset_category(self.project, "NOPE", self.owner)

    def test_consequences_are_derived_from_the_category_and_have_no_universal_validation_flag(self):
        for category, _ in CATEGORIES:
            consequences = category_consequences(category)
            self.assertEqual(consequences["supplier_documents"], "SPECIFIC_SUPPLIER_CIRCUIT")
            self.assertNotIn("internal_validation_required", consequences)
            self.assertNotIn("internal_validation_required", consequences["internal_documents"])
        a, b, c, d = (category_consequences(x)["internal_documents"] for x, _ in CATEGORIES)
        self.assertEqual((a["new_revision_required"], a["new_distribution_required"], a["internal_validation"]), (False, True, "NONE"))
        self.assertEqual((b["new_revision_required"], b["internal_validation"], b["redistribution_for_review"]), (True, "APPLICABLE_WORKFLOW", False))
        self.assertEqual((c["internal_validation"], c["redistribution_for_review"]), ("APPLICABLE_WORKFLOW", True))
        self.assertEqual((d["internal_validation"], d["redistribution_for_review"]), ("APPLICABLE_WORKFLOW", True))


class ApiTests(CodesBase):
    def test_read_follows_documents_access_and_is_isolated_by_project_and_company(self):
        reader = self.reader()
        response = self.call("get", reader, self.project)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["codes"]), 4)
        self.assertEqual(self.call("get", reader, self.other_project).status_code, 403)  # no membership there
        stranger = make_user("wf41.stranger@links-local.invalid")
        stranger.company = self.other_company
        stranger.save(update_fields=["company"])
        self.assertEqual(self.call("get", stranger, self.project).status_code, 403)
        nobody = self.grant("wf41.nodocs@links-local.invalid", self.project, ["documents.transmittal_view"])
        self.assertEqual(self.call("get", nobody, self.project).status_code, 403)
        self.assertEqual(self.call("get", self.admin(), self.project).status_code, 200)

    def test_write_needs_the_project_configuration_right(self):
        body = {"codes": [{"category": "ACCEPTED_WITH_COMMENTS", "code": "B2", "label_en": "With comments"}]}
        for user, expected in (
            (self.reader(), 403),  # may read, may not write
            (self.grant("wf41.update@links-local.invalid", self.project, ["documents.update"]), 403),  # documents.update is not enough
            (self.admin(), 200),
        ):
            response = self.call("put", user, self.project, action="save", body=body)
            self.assertEqual(response.status_code, expected, user.email)
        self.assertEqual(ClientSurveillanceCode.objects.get().code, "B2")
        # configuration right held in another project only
        elsewhere = self.admin(self.other_project)
        self.assertEqual(self.call("put", elsewhere, self.project, action="save", body=body).status_code, 403)
        self.assertEqual(self.call("put", elsewhere, self.other_project, action="save", body=body).status_code, 200)
        self.assertEqual(ClientSurveillanceCode.objects.filter(project=self.project).count(), 1)

    def test_full_access_on_configuration_is_recognised(self):
        user = self.grant("wf41.full@links-local.invalid", self.project, ["ecpmp_configurations.full_access"])
        body = {"codes": [{"category": "REJECTED", "code": "R"}]}
        self.assertEqual(self.call("put", user, self.project, action="save", body=body).status_code, 200)

    def test_patch_one_category_and_reset_endpoint(self):
        admin = self.admin()
        response = self.call("patch", admin, self.project, category="COMMENTED", action="update_one",
                             body={"code": "C2", "label_fr": "Commenté (FR)", "label_de": "Kommentiert (DE)"})
        self.assertEqual(response.status_code, 200)
        row = self.codes(response)["COMMENTED"]
        self.assertEqual((row["code"], row["label_fr"], row["label_de"], row["customized"]), ("C2", "Commenté (FR)", "Kommentiert (DE)", True))
        self.assertEqual(self.call("post", self.reader(), self.project, category="COMMENTED", action="reset").status_code, 403)
        reset = self.call("post", admin, self.project, category="COMMENTED", action="reset")
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(self.codes(reset)["COMMENTED"]["code"], "C")
        self.assertEqual(self.call("post", admin, self.project, category="NOPE", action="reset").status_code, 400)

    def test_the_category_and_the_business_rules_cannot_be_modified_through_the_api(self):
        admin = self.admin()
        response = self.call("patch", admin, self.project, category="COMMENTED", action="update_one",
                             body={"code": "C3", "new_category": "REJECTED", "consequences": {"internal_documents": {"new_revision_required": False}},
                                   "new_revision_required": False})
        self.assertEqual(response.status_code, 200)
        rows = self.codes(response)
        self.assertEqual(ClientSurveillanceCode.objects.get().category, "COMMENTED")
        self.assertTrue(rows["COMMENTED"]["consequences"]["internal_documents"]["new_revision_required"])

    def test_validation_errors_are_clean_400s_and_nothing_is_written(self):
        admin = self.admin()
        for body in ({"codes": []}, {"codes": "x"}, {"codes": [{"category": "ACCEPTED", "code": "TOO-LONG-CODE"}]},
                     {"codes": [{"category": "ACCEPTED", "code": "B"}]}):  # B is taken by another category
            self.assertEqual(self.call("put", admin, self.project, action="save", body=body).status_code, 400, body)
        self.assertEqual(ClientSurveillanceCode.objects.count(), 0)
        request = APIRequestFactory().get(URL)  # project missing
        force_authenticate(request, user=admin)
        self.assertEqual(ClientSurveillanceCodeViewSet.as_view({"get": "list"}, permission_classes=[])(request).status_code, 400)

    def test_there_is_no_delete_route(self):
        from django.urls import resolve, Resolver404
        resolved = 0
        for path in ("/api/project_management/client-surveillance-codes", "/api/project_management/client-surveillance-codes/COMMENTED",
                     "/api/project_management/client-surveillance-codes/COMMENTED/reset"):
            try:
                match = resolve(path)
            except Resolver404:
                continue
            resolved += 1
            self.assertNotIn("delete", [m.lower() for m in match.func.actions])
        self.assertEqual(resolved, 3)  # the routes really exist, and none of them deletes


class PreservationTests(CodesBase):
    def test_existing_surveillances_and_other_project_settings_are_untouched(self):
        document, revision = self.supplier_like_revision()
        issue = ClientIssue.objects.create(document_revision=revision, client_company=self.client_company)
        surveillance = ClientSurveillance.objects.create(client_issue=issue, status="CLOSED", result="COMMENTED", comment="to fix")
        before = (surveillance.status, surveillance.result, surveillance.comment, issue.client_revision_label)
        save_codes(self.project, [{"category": "COMMENTED", "code": "C7", "label_en": "Reworked"}], self.owner)
        reset_category(self.project, "COMMENTED", self.owner)
        surveillance.refresh_from_db()
        issue.refresh_from_db()
        self.assertEqual((surveillance.status, surveillance.result, surveillance.comment, issue.client_revision_label), before)
        self.assertEqual(ClientSurveillance.objects.count(), 1)

    def test_the_review_durations_configuration_is_unchanged(self):
        from project_management.services.document_type_review_duration import list_effective_review_durations
        before = list_effective_review_durations(self.project)
        save_codes(self.project, [{"category": "ACCEPTED", "code": "Q"}], self.owner)
        self.assertEqual(list_effective_review_durations(self.project), before)

    def supplier_like_revision(self):
        from project_management.models import DocumentOrigin
        from project_management.services.document_revision import add_document_revision
        from django.core.files.uploadedfile import SimpleUploadedFile
        document = self.make_document(origin=DocumentOrigin.INTERNAL, name="plan.txt")
        revision = add_document_revision(document, SimpleUploadedFile("r.txt", b"x"), self.owner)
        return document, revision
