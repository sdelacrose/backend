"""GED-SEC.08.2-B0.3: pending -> final transfer, binding of finalised references to business fields, private read link.

Same isolation as B0.2: temporary test database, in-memory fake S3 (`boto3.client` raises if a real client is built),
synthetic users and files, licence SQL stubbed. No migration is involved: a final key exists only after a successful
finalisation (the browser can write under the pending prefix only), which is the proof the binding relies on."""
import json
import types

from django.test import override_settings
from rest_framework import serializers
from rest_framework.test import APIRequestFactory, force_authenticate

from companyprofileapis.serializers.company import UserSerializer
from project_management.models import Observation, ProjectTeamMembers, SiteReport
from project_management.serializers.contact import ContactCompanySerializer
from project_management.serializers.project import ProjectSerializer
from project_management.serializers.site_reporting import SiteReportSerializer
from project_management.services import upload_policy as policy
from project_management.services.upload_reference import INVALID, bind_private_reference, bind_private_references, bind_public_url
from project_management.test_ged_sec082_b02_secure_upload import PDF, PNG, UploadBase
from project_management.views.secure_upload import UploadReadView
from userapis.serializers.feedback import FeedbackSerializer

READ = UploadReadView.as_view()
IMAGE = {"filename": "a.png", "content_type": "image/png", "size_bytes": len(PNG)}


class TransferBase(UploadBase):
    def done(self, user, usage, project_id=..., **file):
        """Prepare, post to the pending key, finalise: what a browser does. Returns the finalize body."""
        spec = {**IMAGE, **file}
        prepared = json.loads(self.prepare(user, usage=usage, project_id=project_id, **spec).content)
        self.put_object(prepared, data=PNG if spec["filename"].endswith("png") else PDF, content_type=spec["content_type"])
        response = self.finalize(user, prepared["upload_id"])
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def context(self, user):
        return {"request": types.SimpleNamespace(user=user)}


class PendingFinalTests(TransferBase):
    def test_1_2_a_pending_key_and_a_final_key_never_share_a_prefix(self):
        prepared = json.loads(self.prepare(self.member).content)
        pending, final = prepared["fields"]["key"], self.ticket(prepared)["key"]
        self.assertTrue(pending.startswith(policy.pending_prefix()) and policy.is_pending_key(pending))
        self.assertFalse(policy.is_pending_key(final))
        self.assertEqual(policy.pending_prefix(), "uploads/pending/")
        for usage, expected in (("project_image", "public"), ("contact_company_logo", "public"), ("user_avatar", "public"),
                                ("feedback_attachment", "private"), ("site_report_photo", "private"), ("object_assessment_file", "private")):
            self.assertEqual(policy.USAGES[usage].visibility, expected, usage)
        # an environment can never be mistaken for the pending prefix, which would put final files under the lifecycle rule
        for name in ("pending", "public", "private"):
            with override_settings(ENV=name):
                self.assertNotIn(f"/{name}/", policy.build_pending_key("project_image", "png").replace("uploads/pending/", "", 1) + "/")
                self.assertFalse(policy.is_pending_key(policy.build_final_key(policy.USAGES["project_image"], "company_1", None, "png")))

    def test_3_the_transfer_copies_with_the_servers_type_then_deletes_the_pending_object_after_the_copy(self):
        prepared = json.loads(self.prepare(self.member).content)
        self.put_object(prepared)
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 200)
        kinds = [call[0] for call in self.s3.calls]
        copy = self.last("copy")
        self.assertEqual(copy[1:], (prepared["fields"]["key"], self.ticket(prepared)["key"], "application/pdf", "inline", "REPLACE"))
        self.assertLess(kinds.index("copy"), kinds.index("delete"))  # never deleted before the copy

    def test_4_a_public_usage_gets_its_public_url_a_private_one_never(self):
        public = self.done(self.company_user, "project_image", **IMAGE)
        self.assertRegex(public["reference"], rf"^uploads/public/test/project_image/company_{self.company.pk}/[0-9a-f]{{32}}\.png$")
        self.assertEqual(public["public_url"], f"https://test-bucket.s3.amazonaws.com/{public['reference']}")
        private = self.done(self.reader, "feedback_attachment", **IMAGE)
        self.assertRegex(private["reference"], rf"^uploads/private/test/feedback_attachment/user_{self.reader.pk}/[0-9a-f]{{32}}\.png$")
        self.assertIsNone(private["public_url"])

    def test_5_the_public_base_can_be_a_future_cdn_domain(self):
        with override_settings(SECURE_UPLOAD_PUBLIC_BASE_URL="https://cdn.example.test/"):
            self.assertEqual(self.done(self.reader, "user_avatar", **IMAGE)["public_url"].split("/uploads/")[0], "https://cdn.example.test")

    def test_6_an_avatar_belongs_to_its_user_not_to_the_company(self):
        reference = self.done(self.reader, "user_avatar", **IMAGE)["reference"]
        self.assertIn(f"/user_{self.reader.pk}/", reference)

    def test_7_a_failed_copy_keeps_the_pending_object_and_attaches_nothing_and_can_be_retried(self):
        prepared = json.loads(self.prepare(self.member).content)
        self.put_object(prepared)
        self.s3.fail_copy = True
        response = self.finalize(self.member, prepared["upload_id"])
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.content.decode())
        self.assertIn(prepared["fields"]["key"], self.s3.objects)
        self.assertNotIn(self.ticket(prepared)["key"], self.s3.objects)
        self.s3.fail_copy = False
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 200)

    def test_8_a_failed_delete_after_the_copy_still_finalises_and_a_retry_cleans_up(self):
        prepared = json.loads(self.prepare(self.member).content)
        self.put_object(prepared)
        self.s3.fail_delete = True
        with self.assertLogs("project_management.services.upload_s3", level="WARNING") as logs:
            response = self.finalize(self.member, prepared["upload_id"])
        self.assertEqual(response.status_code, 200)
        self.assertNotRegex(" ".join(logs.output), "SECRET")
        self.assertIn(prepared["fields"]["key"], self.s3.objects)  # left for the lifecycle rule, never attached
        self.s3.fail_delete = False
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 200)
        self.assertNotIn(prepared["fields"]["key"], self.s3.objects)

    def test_9_a_repeated_finalisation_never_copies_twice(self):
        prepared = json.loads(self.prepare(self.member).content)
        self.put_object(prepared)
        for _ in range(3):
            self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 200)
        self.assertEqual(len([call for call in self.s3.calls if call[0] == "copy"]), 1)

    def test_10_a_copy_that_is_not_what_was_checked_is_removed_and_not_attached(self):
        prepared = json.loads(self.prepare(self.member).content)
        self.put_object(prepared)
        original = self.s3.copy_object

        def truncated_copy(**kwargs):
            original(**kwargs)
            self.s3.objects[kwargs["Key"]] = (b"%PDF-", kwargs["ContentType"])  # the destination differs from the source

        self.s3.copy_object = truncated_copy
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 503)
        self.assertNotIn(self.ticket(prepared)["key"], self.s3.objects)

    def test_11_a_final_object_that_became_invalid_is_refused_on_a_repeated_call(self):
        prepared = json.loads(self.prepare(self.member).content)
        self.put_object(prepared)
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 200)
        self.s3.objects[self.ticket(prepared)["key"]] = (b"<html>", "application/pdf")
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 400)
        self.assertNotIn(self.ticket(prepared)["key"], self.s3.objects)

    def test_12_another_user_cannot_finalise_the_upload_of_somebody_else(self):
        prepared = json.loads(self.prepare(self.member).content)
        self.put_object(prepared)
        self.assertEqual(self.finalize(self.reader, prepared["upload_id"]).status_code, 400)  # not the owner of the ticket
        self.assertNotIn(self.ticket(prepared)["key"], self.s3.objects)

    def test_13_a_finalised_object_is_never_under_the_pending_prefix_so_a_lifecycle_rule_cannot_reach_it(self):
        keys = [self.done(self.company_user, "project_image", **IMAGE)["reference"],
                self.done(self.reader, "feedback_attachment", **IMAGE)["reference"],
                self.done(self.member, "site_report_photo", project_id=self.project.pk, **IMAGE)["reference"]]
        self.assertFalse(any(policy.is_pending_key(key) for key in keys))
        self.assertTrue(all(policy.parse_final_reference(key) is not None for key in keys))


class PublicBindingTests(TransferBase):
    def test_14_nothing_the_current_value_and_the_own_finalised_upload_are_accepted(self):
        url = self.done(self.company_user, "project_image", **IMAGE)["public_url"]
        user = self.company_user
        self.assertIsNone(bind_public_url(user, "project_image", None))
        self.assertEqual(bind_public_url(user, "project_image", ""), "")
        self.assertEqual(bind_public_url(user, "project_image", url), url)
        self.assertEqual(bind_public_url(user, "project_image", "https://old.example/x.png", current="https://old.example/x.png"), "https://old.example/x.png")

    def test_15_16_an_external_url_a_lookalike_and_a_pending_key_are_refused(self):
        user = self.company_user
        good = self.done(user, "project_image", **IMAGE)
        for value in ("https://evil.example/x.png", good["public_url"] + "?x=1", good["public_url"] + "/", good["public_url"].replace("https://", "http://"),
                      good["reference"], "javascript:alert(1)", 5, ["a"], "https://test-bucket.s3.amazonaws.com/uploads/public/test/project_image/company_1/../x.png",
                      f"https://test-bucket.s3.amazonaws.com/uploads/pending/test/project_image/{'a' * 32}.png"):
            with self.assertRaises(serializers.ValidationError, msg=str(value)):
                bind_public_url(user, "project_image", value)

    def test_17_a_reference_of_another_company_another_user_or_another_usage_is_refused(self):
        mine = self.done(self.company_user, "project_image", **IMAGE)["public_url"]
        other_company = self.company_user_with("up.other.co@links-local.invalid", ["projects.create"])
        other_company.company = self.other_company
        other_company.save(update_fields=["company"])
        with self.assertRaises(serializers.ValidationError):
            bind_public_url(other_company, "project_image", mine)  # another company
        with self.assertRaises(serializers.ValidationError):
            bind_public_url(self.company_user, "contact_company_logo", mine)  # another usage
        avatar = self.done(self.reader, "user_avatar", **IMAGE)["public_url"]
        with self.assertRaises(serializers.ValidationError):
            bind_public_url(self.member, "user_avatar", avatar)  # another user, same company
        self.assertEqual(bind_public_url(self.reader, "user_avatar", avatar), avatar)

    def test_18_a_reference_that_was_never_finalised_is_refused(self):
        user = self.company_user
        url = self.done(user, "project_image", **IMAGE)["public_url"]
        self.s3.objects.pop(url.split("amazonaws.com/")[1])
        with self.assertRaises(serializers.ValidationError):
            bind_public_url(user, "project_image", url)

    def test_19_a_reference_of_another_environment_or_project_is_refused(self):
        url = self.done(self.member, "project_image", project_id=self.project.pk, **IMAGE)["public_url"]
        self.assertEqual(bind_public_url(self.member, "project_image", url, project_id=self.project.pk), url)
        with self.assertRaises(serializers.ValidationError):
            bind_public_url(self.member, "project_image", url, project_id=self.other_project.pk)  # prepared for another project
        with self.assertRaises(serializers.ValidationError):
            bind_public_url(self.member, "project_image", url)  # a key prepared for a project is not for a new one
        with override_settings(ENV="prod"):
            with self.assertRaises(serializers.ValidationError):
                bind_public_url(self.member, "project_image", url, project_id=self.project.pk)

    def test_20_the_five_serializers_apply_the_binding(self):
        mine = self.done(self.company_user, "project_image", **IMAGE)["public_url"]
        context = self.context(self.company_user)
        creating = ProjectSerializer(context=context)
        self.assertEqual(creating.validate_project_image(mine), mine)
        for evil in ("https://evil.example/x.png", "x"):
            with self.assertRaises(serializers.ValidationError):
                creating.validate_project_image(evil)
        editing = ProjectSerializer(instance=self.project, context=context)
        with self.assertRaises(serializers.ValidationError):
            editing.validate_project_image(mine)  # prepared without a project: not attachable to an existing one
        # the contact company logo
        logo = self.done(self.company_user, "contact_company_logo", project_id=..., **IMAGE)["public_url"]
        company = ContactCompanySerializer(instance=self.client_company, context=context)
        self.assertEqual(company.validate_company_logo(logo), logo)
        with self.assertRaises(serializers.ValidationError):
            company.validate_company_logo("https://evil.example/logo.png")
        # the avatar: a user changes his own; nobody changes somebody else's
        avatar = self.done(self.reader, "user_avatar", project_id=..., **IMAGE)["public_url"]
        own = UserSerializer(instance=self.reader, context=self.context(self.reader))
        self.assertEqual(own.validate_user_image(avatar), avatar)
        with self.assertRaises(serializers.ValidationError):
            UserSerializer(instance=self.reader, context=self.context(self.member)).validate_user_image(avatar)
        with self.assertRaises(serializers.ValidationError):
            own.validate_user_image("https://evil.example/me.png")

    def test_21_historical_values_stay_valid_for_their_own_record_only(self):
        self.project.project_image = "https://staging-sharemundo.s3.eu-west-3.amazonaws.com/old-photo.png"
        self.project.save(update_fields=["project_image"])
        context = self.context(self.company_user)
        same = ProjectSerializer(instance=self.project, context=context)
        self.assertEqual(same.validate_project_image(self.project.project_image), self.project.project_image)
        other = ProjectSerializer(instance=self.other_project, context=context)
        with self.assertRaises(serializers.ValidationError):
            other.validate_project_image(self.project.project_image)  # a historical URL cannot be copied elsewhere

    def test_22_no_request_context_means_an_internal_use_and_is_not_blocked(self):
        self.assertEqual(ProjectSerializer().validate_project_image("https://seed.example/x.png"), "https://seed.example/x.png")
        self.assertEqual(ContactCompanySerializer().validate_company_logo("https://seed.example/l.png"), "https://seed.example/l.png")


class PrivateBindingTests(TransferBase):
    def test_23_a_feedback_attachment_must_be_the_own_finalised_reference_or_a_historical_one(self):
        reference = self.done(self.reader, "feedback_attachment", project_id=..., **IMAGE)["reference"]
        serializer = FeedbackSerializer(context=self.context(self.reader))
        self.assertEqual(serializer.validate_attachments([reference]), [reference])
        for bad in (["https://evil.example/x.png"], [reference + "x"], [5], "not a list"):
            with self.assertRaises(serializers.ValidationError, msg=str(bad)):
                serializer.validate_attachments(bad)
        with self.assertRaises(serializers.ValidationError):
            FeedbackSerializer(context=self.context(self.member)).validate_attachments([reference])  # a colleague of the same company
        self.assertEqual(bind_private_references(self.reader, "feedback_attachment", ["https://old/x.png"], existing=["https://old/x.png"]), ["https://old/x.png"])

    def test_24_a_private_reference_is_never_accepted_as_a_public_url_and_vice_versa(self):
        private = self.done(self.reader, "feedback_attachment", project_id=..., **IMAGE)["reference"]
        with self.assertRaises(serializers.ValidationError):
            bind_public_url(self.reader, "feedback_attachment", f"https://test-bucket.s3.amazonaws.com/{private}")
        public = self.done(self.company_user, "project_image", **IMAGE)["reference"]
        with self.assertRaises(serializers.ValidationError):
            bind_private_reference(self.company_user, "project_image", public)

    def test_25_a_site_report_photo_is_bound_to_the_project_of_the_report(self):
        reference = self.done(self.member, "site_report_photo", project_id=self.project.pk, **IMAGE)["reference"]
        observer = ProjectTeamMembers.objects.get(project=self.project, team_member=self.member)
        report = SiteReport.objects.create(project=self.project, observer=observer, created_by=self.member)
        Observation.objects.create(site_report=report, contractor=observer, observation="o", photos=["https://old.example/p.png"])
        context = self.context(self.member)

        def check(photos, instance=None, project=None):
            serializer = SiteReportSerializer(instance=instance, context=context)
            return serializer.validate({"project": project or self.project, "observations": [{"photos": list(photos)}]})

        self.assertEqual(check([reference])["observations"][0]["photos"], [reference])
        self.assertEqual(check(["https://old.example/p.png", reference], instance=report)["observations"][0]["photos"], ["https://old.example/p.png", reference])
        with self.assertRaises(serializers.ValidationError):
            check(["https://old.example/p.png"])  # a historical photo only next to the report that holds it
        with self.assertRaises(serializers.ValidationError):
            check(["https://evil.example/x.png"], instance=report)
        with self.assertRaises(serializers.ValidationError):
            check([reference], project=self.other_project)  # prepared for another project

    def test_26_the_failure_message_is_generic(self):
        with self.assertRaises(serializers.ValidationError) as caught:
            bind_private_reference(self.reader, "feedback_attachment", "uploads/private/test/feedback_attachment/company_9/x")
        self.assertEqual(caught.exception.detail, [INVALID])
        self.assertNotIn("uploads/", str(caught.exception.detail))


class ReadLinkTests(TransferBase):
    def read(self, user, reference):
        request = APIRequestFactory().get("/x/", {"reference": reference} if reference is not None else {})
        if user is not None:
            force_authenticate(request, user=user)
        response = READ(request)
        response.render()
        return response

    def test_27_the_owner_gets_a_short_lived_link_never_a_stored_url(self):
        reference = self.done(self.reader, "feedback_attachment", project_id=..., **IMAGE)["reference"]
        response = self.read(self.reader, reference)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertIn("X-FAKE-SIGNATURE", body["url"])
        self.assertEqual(self.last("presign_get")[3], 300)
        self.assertIn("expires_at", body)

    def test_28_everything_else_is_the_same_404(self):
        reference = self.done(self.reader, "feedback_attachment", project_id=..., **IMAGE)["reference"]
        stranger = self.company_user_with("up.stranger@links-local.invalid", [])
        stranger.company = self.other_company
        stranger.save(update_fields=["company"])
        public = self.done(self.company_user, "project_image", **IMAGE)["reference"]
        answers = {self.read(stranger, reference).status_code, self.read(self.reader, reference + "0").status_code,
                   self.read(self.reader, "uploads/private/test/feedback_attachment/company_1/" + "a" * 32 + ".png").status_code,
                   self.read(self.reader, public).status_code, self.read(self.reader, "https://evil.example/x.png").status_code,
                   self.read(self.reader, None).status_code}
        self.assertEqual(answers, {404})
        self.assertEqual(self.read(None, reference).status_code, 401)

    def test_29_a_project_photo_needs_the_project_and_its_view_right(self):
        reference = self.done(self.member, "site_report_photo", project_id=self.project.pk, **IMAGE)["reference"]
        self.assertEqual(self.read(self.member, reference).status_code, 404)  # member without site_reporting.view
        viewer = self.project_user("up.viewer@links-local.invalid", self.project, ["site_reporting.view"])
        self.assertEqual(self.read(viewer, reference).status_code, 200)
        self.assertEqual(self.read(self.reader, reference).status_code, 404)  # a member without that right

    def test_30_a_missing_object_is_a_404_and_no_link_is_made(self):
        reference = self.done(self.reader, "feedback_attachment", project_id=..., **IMAGE)["reference"]
        self.s3.objects.pop(reference)
        self.s3.calls.clear()
        self.assertEqual(self.read(self.reader, reference).status_code, 404)
        self.assertFalse([call for call in self.s3.calls if call[0] == "presign_get"])

