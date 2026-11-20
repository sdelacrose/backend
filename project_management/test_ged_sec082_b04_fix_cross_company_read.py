"""GED-SEC.08.2-B0.4-FIX (B04-A01): a site report photo is read by the members of a project shared by several companies.

The rule under test: scope of the project (the reader belongs to its team or company) AND the business right to view site
reports AND the uploader is a legitimate party of that very project. Same company alone is not enough, team membership
alone is not enough. Every other private usage keeps its own owner rule. Fake S3 only: a real AWS client would raise.
"""
import json
import types
from unittest.mock import patch

from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, Permission, RolePermission
from project_management.models import ProjectTeamMembers
from project_management.services import site_report_pdf_photos as photos
from project_management.services import upload_policy as policy
from project_management.test_ged_sec082_b02_secure_upload import PNG
from project_management.test_ged_sec082_b03_fix_site_report_pdf import PdfBase, image, pdf_images
from project_management.test_transmittal import make_project, make_user
from project_management.views.secure_upload import UploadReadView

READ = UploadReadView.as_view()
VIEW = ["site_reporting.view"]


class CrossCompanyReadTests(PdfBase):
    def setUp(self):
        super().setUp()
        self.a_viewer = self.project_user("a.viewer@links-local.invalid", self.project, VIEW)
        self.b_member = self.team_user("b.member@links-local.invalid", self.other_company, self.project, VIEW + ["site_reporting.create"])
        self.b_no_right = self.team_user("b.noright@links-local.invalid", self.other_company, self.project, ["object_assessments.view"])
        c_owner = make_user("c.owner@links-local.invalid")
        self.company_c = CompanyProfile.objects.create(
            admin_user=c_owner, company_name="Company C", company_address="1", post_code="0", company_town="T", company_country="C",
            company_tell_no="0", company_mob_no="1", company_web_address="http://x", company_email_address="c@x.invalid",
            company_tax_reference="T", company_iban="I")
        c_owner.company = self.company_c
        c_owner.save(update_fields=["company"])
        self.c_user = self.company_user_in(self.company_c, "c.user@links-local.invalid", VIEW)
        self.a_nonmember = self.company_user_in(self.company, "a.nonmember@links-local.invalid", VIEW)  # same company, not on the team

    # --- fixtures ------------------------------------------------------------------------------------------------
    def team_user(self, email, company, project, keys):
        user = make_user(email)
        user.company = company
        user.save(update_fields=["company"])
        ProjectTeamMembers.objects.update_or_create(project=project, team_member=user, defaults={
            "role": "Member", "project_role": self.role(user, "project", keys), "project_access": "RW"})
        return user

    def company_user_in(self, company, email, keys):
        from models.models import UserRole

        user = make_user(email)
        user.company = company
        user.save(update_fields=["company"])
        UserRole.objects.create(user=user, role=self.role(user, "company", keys), company=company, assigned_by=user)
        return user

    def read(self, user, reference):
        request = APIRequestFactory().get("/x/", {"reference": reference} if reference is not None else {})
        if user is not None:
            force_authenticate(request, user=user)
        response = READ(request)
        response.render()
        return response

    def photo(self, company, **where):
        return self.put(PNG, company=company, **where)

    # --- 1-4: shared project -------------------------------------------------------------------------------------
    def test_01_the_owner_company_reads_its_own_photo(self):
        self.assertEqual(self.read(self.a_viewer, self.photo(self.company)).status_code, 200)

    def test_02_an_authorised_member_of_another_company_reads_it(self):
        self.assertEqual(self.read(self.b_member, self.photo(self.company)).status_code, 200)

    def test_03_and_the_other_way_round(self):
        self.assertEqual(self.read(self.a_viewer, self.photo(self.other_company)).status_code, 200)
        self.assertEqual(self.read(self.b_member, self.photo(self.other_company)).status_code, 200)

    def test_04_a_company_that_is_not_on_the_project_reads_nothing(self):
        for key in (self.photo(self.company), self.photo(self.other_company)):
            self.assertEqual(self.read(self.c_user, key).status_code, 404)

    def test_05_a_user_outside_the_project_is_refused_even_in_the_owner_company(self):
        self.assertEqual(self.read(self.a_nonmember, self.photo(self.company)).status_code, 404)

    def test_06_a_team_member_without_the_business_right_is_refused(self):
        self.assertEqual(self.read(self.b_no_right, self.photo(self.company)).status_code, 404)
        self.assertEqual(self.read(self.reader, self.photo(self.company)).status_code, 404)  # members with object assessments only

    def test_08_a_right_withdrawn_after_the_upload_is_refused(self):
        key = self.photo(self.company)
        self.assertEqual(self.read(self.b_member, key).status_code, 200)
        permission = Permission.objects.get(key="site_reporting.view")
        role = ProjectTeamMembers.objects.get(project=self.project, team_member=self.b_member).project_role
        RolePermission.objects.filter(role=role, permission=permission).delete()
        self.assertEqual(self.read(self.b_member, key).status_code, 404)

    def test_08b_a_member_removed_from_the_team_is_refused(self):
        key = self.photo(self.company)
        ProjectTeamMembers.objects.filter(project=self.project, team_member=self.b_member).delete()
        self.assertEqual(self.read(self.b_member, key).status_code, 404)

    # --- 9-12: references that are not what they claim -----------------------------------------------------------
    def test_09_another_project_is_refused(self):
        other = make_project(self.company, self.member, "Second project")  # same company, the viewer is not on its team
        self.assertEqual(self.read(self.a_viewer, self.photo(self.company, project=other)).status_code, 404)
        self.assertEqual(self.read(self.b_member, self.photo(self.company, project=other)).status_code, 404)
        self.assertEqual(self.read(self.a_viewer, self.photo(self.other_company, project=self.other_tenant_project)).status_code, 404)

    def test_10_another_usage_keeps_its_owner_rule(self):
        key = self.photo(self.company, usage="object_assessment_file")
        oa_member = self.team_user("b.oa@links-local.invalid", self.other_company, self.project, ["object_assessments.view"])
        own = self.team_user("a.oa@links-local.invalid", self.company, self.project, ["object_assessments.view"])
        self.assertEqual(self.read(oa_member, key).status_code, 404)  # another company: unchanged, not opened by this fix
        self.assertEqual(self.read(own, key).status_code, 200)

    def test_11_another_environment_is_refused(self):
        self.assertEqual(self.read(self.b_member, self.photo(self.company, env="prod")).status_code, 404)

    def test_12_a_forged_reference_is_refused(self):
        genuine = self.photo(self.company)
        forged = {
            "owner not on the project": self.photo(self.company_c),
            "owner changed in a genuine key": genuine.replace(f"company_{self.company.pk}", f"company_{self.company_c.pk}"),
            "no project segment": f"uploads/private/test/site_report_photo/company_{self.company.pk}/{'a' * 32}.png",
            "pending key": "uploads/pending/test/site_report_photo/" + "a" * 32 + ".png",
            "public visibility": genuine.replace("/private/", "/public/"),
            "path detour": genuine.replace("site_report_photo", "site_report_photo/../x"),
            "url": "https://evil.example/x.png",
        }
        for label, key in forged.items():
            with self.subTest(label):
                self.assertEqual(self.read(self.b_member, key).status_code, 404)
        self.assertEqual(self.read(None, genuine).status_code, 401)

    def test_12b_a_user_uploader_segment_of_a_team_user_is_legitimate_but_a_stranger_is_not(self):
        member_key = f"uploads/private/test/site_report_photo/user_{self.b_member.pk}/project_{self.project.pk}/{'b' * 32}.png"
        stranger_key = f"uploads/private/test/site_report_photo/user_{self.c_user.pk}/project_{self.project.pk}/{'c' * 32}.png"
        self.s3.objects[member_key] = (PNG, "image/png")
        self.s3.objects[stranger_key] = (PNG, "image/png")
        self.assertEqual(self.read(self.a_viewer, member_key).status_code, 200)
        self.assertEqual(self.read(self.a_viewer, stranger_key).status_code, 404)

    # --- 13-16 ---------------------------------------------------------------------------------------------------
    def test_13_a_private_feedback_attachment_is_still_the_authors_alone(self):
        key = f"uploads/private/test/feedback_attachment/user_{self.a_viewer.pk}/{'f' * 32}.png"
        self.s3.objects[key] = (PNG, "image/png")
        self.assertEqual(self.read(self.a_viewer, key).status_code, 200)
        for other in (self.b_member, self.member, self.a_nonmember):
            self.assertEqual(self.read(other, key).status_code, 404)

    def test_15_no_storage_key_in_the_answers(self):
        key = self.photo(self.company)
        ok = self.read(self.b_member, key)
        self.assertEqual(set(json.loads(ok.content)), {"url", "expires_at"})
        self.assertNotIn(b"uploads/", self.read(self.c_user, key).content)

    def test_16_the_read_link_is_short_lived_and_only_the_fake_storage_was_used(self):
        self.read(self.b_member, self.photo(self.company))
        self.assertEqual(self.last("presign_get")[3], 300)

    # --- the PDF generation and the direct read answer the same question ------------------------------------------
    def test_17_the_pdf_and_the_direct_read_agree_on_who_is_a_party_of_the_project(self):
        report = self.report([])
        for owner_company, expected in ((self.company, True), (self.other_company, True), (self.company_c, False)):
            key = self.photo(owner_company)
            resolver = photos.SiteReportPhotoResolver(report, self.a_viewer)
            if expected:
                self.assertTrue(resolver.register(key))
            else:
                with self.assertRaises(photos.PhotoForbidden):
                    resolver.register(key)
            self.assertEqual(self.read(self.a_viewer, key).status_code, 200 if expected else 404)

    def test_18_pdf_generation_with_the_photos_of_both_companies(self):
        report = self.report([self.put(image(), company=self.company), self.put(image(size=(30, 50)), company=self.other_company)])
        document, pdf = self.generate(report, self.member)
        self.assertEqual(len(pdf_images(pdf)), 2)

    def test_19_the_team_owner_set_is_the_project_company_the_owner_and_the_team(self):
        owners = policy.project_team_owners(self.project)
        self.assertIn(f"company_{self.company.pk}", owners)
        self.assertIn(f"company_{self.other_company.pk}", owners)
        self.assertNotIn(f"company_{self.company_c.pk}", owners)
        self.assertNotIn("company_None", owners)
