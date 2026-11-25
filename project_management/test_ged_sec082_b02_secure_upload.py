"""GED-SEC.08.2-B0.2: authenticated, server-controlled uploads (prepare / finalize) and the hardened legacy endpoint.

No AWS call is possible here: the S3 client is replaced by an in-memory fake and `boto3.client` itself raises if anything
tries to build a real one. Requests go through the real view classes (force_authenticate) and, for the authentication
checks, through the URL router with the real JWT authentication class. Only the licence SQL (tables owned by the admin
server, absent from the test database) is stubbed. Every file is synthetic."""
import base64
import io
import json
import re
import time
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from ecpmpapp.custom_exceptions import InactiveLicenseException
from models.models import Permission, Role, RolePermission, UserRole
from project_management.models import ProjectTeamMembers
from project_management.services import upload_policy as policy
from project_management.test_transmittal import TransmittalTestBase, make_user
from project_management.views.project import ProjectViewSet
from project_management.views.secure_upload import UploadFinalizeView, UploadPrepareView

LICENCE = "ecpmpapp.permissions.LicensePermission.has_permission"
S3 = "project_management.services.upload_s3.get_s3_client"
PREPARE, FINALIZE = UploadPrepareView.as_view(), UploadFinalizeView.as_view()
LEGACY = ProjectViewSet.as_view({"post": "upload_file_to_s3"}, **ProjectViewSet.upload_file_to_s3.kwargs)
PDF = b"%PDF-1.4 synthetic"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic"


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """An in-memory S3: it signs nothing and stores nothing outside the process."""

    def __init__(self):
        self.objects, self.calls, self.fail_with = {}, [], None
        self.fail_copy = self.fail_delete = self.fail_get = False

    def generate_presigned_post(self, Bucket, Key, Fields=None, Conditions=None, ExpiresIn=3600):
        self.calls.append(("presign", Bucket, Key, dict(Fields or {}), list(Conditions or []), ExpiresIn))
        return {"url": f"https://{Bucket}.s3.amazonaws.com/", "fields": {**(Fields or {}), "key": Key, "policy": "FAKE", "x-amz-signature": "FAKE"}}

    def generate_presigned_url(self, ClientMethod, Params=None, ExpiresIn=3600):
        self.calls.append(("presign_get", ClientMethod, dict(Params or {}), ExpiresIn))
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?X-FAKE-SIGNATURE=1"

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("404")
        data, content_type = self.objects[Key]
        return {"ContentLength": len(data), "ContentType": content_type}

    def get_object(self, Bucket, Key, Range=None):
        self.calls.append(("get", Key))
        if self.fail_get:
            raise RuntimeError("SECRET-PROVIDER-DETAIL read failed")
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def copy_object(self, Bucket, Key, CopySource, ContentType, ContentDisposition, MetadataDirective):
        self.calls.append(("copy", CopySource["Key"], Key, ContentType, ContentDisposition, MetadataDirective))
        if self.fail_copy:
            raise RuntimeError("SECRET-PROVIDER-DETAIL copy failed")
        self.objects[Key] = (self.objects[CopySource["Key"]][0], ContentType)

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete", Key))
        if self.fail_delete:
            raise RuntimeError("SECRET-PROVIDER-DETAIL delete failed")
        self.objects.pop(Key, None)

    def upload_fileobj(self, fileobj, Bucket, Key, ExtraArgs=None):
        if self.fail_with:
            raise self.fail_with
        self.calls.append(("upload", Bucket, Key, dict(ExtraArgs or {})))
        self.objects[Key] = (fileobj.read(), (ExtraArgs or {}).get("ContentType"))


@override_settings(AWS_STORAGE_BUCKET_NAME="test-bucket", AWS_S3_CUSTOM_DOMAIN="test-bucket.s3.amazonaws.com", AWS_S3_REGION_NAME="eu-west-3", ENV="test")
class UploadBase(TransmittalTestBase):
    def setUp(self):
        for target, kwargs in ((LICENCE, {"return_value": True}), ("boto3.client", {"side_effect": AssertionError("a real AWS client was built")})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.s3 = FakeS3()
        patcher = patch(S3, return_value=self.s3)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.member = self.project_user("up.member@links-local.invalid", self.project, [
            "object_assessments.create", "site_reporting.create", "project.update"])
        self.reader = self.project_user("up.reader@links-local.invalid", self.project, ["object_assessments.view"])
        self.company_user = self.company_user_with("up.contacts@links-local.invalid", ["contact_management.create", "projects.create"])

    # --- fixtures ----------------------------------------------------------------------------------------------
    def role(self, user, scope, keys):
        role = Role.objects.create(name=f"UP {scope} {user.id}-{len(keys)}", company=user.company, type="solution",
                                   scope=scope, is_default=False, is_active=True, created_by=user)
        for key in keys:
            perm, _ = Permission.objects.get_or_create(key=key, defaults={"name": key, "module": key.split(".")[0], "description": "t"})
            RolePermission.objects.get_or_create(role=role, permission=perm)
        return role

    def project_user(self, email, project, keys):
        user = make_user(email)
        user.company = project.company_profile
        user.save(update_fields=["company"])
        ProjectTeamMembers.objects.update_or_create(project=project, team_member=user, defaults={
            "role": "Member", "project_role": self.role(user, "project", keys), "project_access": "RW"})
        return user

    def company_user_with(self, email, keys):
        user = make_user(email)
        user.company = self.company
        user.save(update_fields=["company"])
        UserRole.objects.create(user=user, role=self.role(user, "company", keys), company=self.company, assigned_by=user)
        return user

    def prepare(self, user, **overrides):
        payload = {"usage": "object_assessment_file", "project_id": self.project.pk, "filename": "report.pdf",
                   "content_type": "application/pdf", "size_bytes": len(PDF)}
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not ...}
        request = APIRequestFactory().post("/x/", payload, format="json")
        if user is not None:
            force_authenticate(request, user=user)
        response = PREPARE(request)
        response.render()
        return response

    def finalize(self, user, upload_id):
        request = APIRequestFactory().post("/x/", {"upload_id": upload_id}, format="json")
        force_authenticate(request, user=user)
        response = FINALIZE(request)
        response.render()
        return response

    def last(self, kind):
        return [call for call in self.s3.calls if call[0] == kind][-1]

    def ticket(self, prepared):
        """The decoded ticket of a prepared upload: the pending and the final key (the browser only sees the former
        through the form fields; the ticket is signed, not secret)."""
        from django.core import signing
        return signing.loads(prepared["upload_id"], salt=policy.TICKET_SALT)

    def put_object(self, prepared, data=PDF, content_type="application/pdf"):
        """What the browser does: post the file to the PENDING key of the form."""
        self.s3.objects[prepared["fields"]["key"]] = (data, content_type)

    def legacy(self, user, name="report.pdf", content=PDF, content_type="application/pdf"):
        request = APIRequestFactory().post("/x/", {"file": SimpleUploadedFile(name, content, content_type)}, format="multipart")
        if user is not None:
            force_authenticate(request, user=user)
        response = LEGACY(request)
        response.render()
        return response


class PrepareAuthenticationTests(UploadBase):
    def test_01_an_anonymous_request_is_refused(self):
        self.assertEqual(self.prepare(None).status_code, 401)
        self.assertEqual(APIClient().post("/api/project_management/uploads/prepare", {}, format="json").status_code, 401)

    def test_02_an_invalid_session_is_refused_by_the_real_authentication_class(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not.a.token")
        for path in ("prepare", "finalize"):
            self.assertEqual(client.post(f"/api/project_management/uploads/{path}", {}, format="json").status_code, 401)
        self.assertEqual(self.s3.calls, [])

    def test_03_an_inactive_licence_is_refused_with_the_licence_contract(self):
        with patch(LICENCE, side_effect=InactiveLicenseException()):
            self.assertEqual(self.prepare(self.member).status_code, 423)
        self.assertEqual(self.s3.calls, [])


class PrepareAuthorisationTests(UploadBase):
    def test_04_a_project_of_another_company_is_the_uniform_404(self):
        response = self.prepare(self.member, project_id=self.other_tenant_project.pk)
        unknown = self.prepare(self.member, project_id=99999999)
        self.assertEqual((response.status_code, unknown.status_code), (404, 404))
        self.assertEqual(response.content, unknown.content)

    def test_05_another_project_of_the_same_company_without_a_role_is_refused(self):
        # same company, no membership and no company-level right: the project-scope permission refuses
        self.assertEqual(self.prepare(self.member, project_id=self.other_project.pk).status_code, 403)

    def test_06_a_member_without_the_permission_of_the_usage_is_refused(self):
        self.assertEqual(self.prepare(self.reader).status_code, 403)
        self.assertEqual(self.prepare(self.reader, usage="site_report_photo", filename="a.png", content_type="image/png").status_code, 403)
        self.assertEqual(self.s3.calls, [])

    def test_06b_the_company_id_must_be_the_users_own(self):
        self.assertEqual(self.prepare(self.member, company_id=self.other_company.pk).status_code, 404)
        self.assertEqual(self.prepare(self.member, company_id=self.company.pk).status_code, 200)

    def test_06c_each_usage_uses_its_own_existing_permission(self):
        self.assertEqual(self.prepare(self.member, usage="site_report_photo", filename="a.png", content_type="image/png").status_code, 200)
        self.assertEqual(self.prepare(self.member, usage="project_image", filename="a.png", content_type="image/png").status_code, 200)
        # contact company logo: the contact_management right at company scope, nothing else
        logo = {"usage": "contact_company_logo", "project_id": ..., "filename": "logo.png", "content_type": "image/png"}
        self.assertEqual(self.prepare(self.company_user, **logo).status_code, 200)
        self.assertEqual(self.prepare(self.member, **logo).status_code, 403)
        # a project is not a valid parameter of a company-level usage
        self.assertEqual(self.prepare(self.company_user, **{**logo, "project_id": self.project.pk}).status_code, 400)
        # a new project (no id yet) needs the right to create one
        new_project = {"usage": "project_image", "project_id": ..., "filename": "p.png", "content_type": "image/png"}
        self.assertEqual(self.prepare(self.company_user, **new_project).status_code, 200)
        self.assertEqual(self.prepare(self.reader, **new_project).status_code, 403)

    def test_06d_an_avatar_is_the_users_own_only(self):
        avatar = {"usage": "user_avatar", "project_id": ..., "filename": "me.png", "content_type": "image/png"}
        self.assertEqual(self.prepare(self.reader, **avatar).status_code, 200)
        self.assertEqual(self.prepare(self.reader, user_id=self.reader.pk, **avatar).status_code, 200)
        self.assertEqual(self.prepare(self.reader, user_id=self.member.pk, **avatar).status_code, 404)

    def test_06e_a_feedback_attachment_follows_the_feedback_rule_and_never_anonymous(self):
        feedback = {"usage": "feedback_attachment", "project_id": ..., "filename": "shot.png", "content_type": "image/png"}
        self.assertEqual(self.prepare(self.reader, **feedback).status_code, 200)
        self.assertEqual(self.prepare(None, **feedback).status_code, 401)


class PrepareValidationTests(UploadBase):
    def test_07_an_unknown_or_unsupported_usage_is_one_refusal(self):
        for usage in ("nope", "legacy_multipart", "company_profile_image", None, 5, ""):
            self.assertEqual(self.prepare(self.member, usage=usage).status_code, 400, usage)

    def test_08_09_the_browser_cannot_name_a_bucket_a_key_a_prefix_an_acl_or_credentials(self):
        for extra in ({"bucket": "other"}, {"key": "x/y.pdf"}, {"prefix": "../"}, {"acl": "public-read"},
                      {"url": "https://evil.example/x"}, {"aws_access_key_id": "x"}, {"Bucket": "b"}):
            self.assertEqual(self.prepare(self.member, **extra).status_code, 400, extra)
        self.assertEqual(self.s3.calls, [])

    def test_10_forbidden_extensions_are_refused_whatever_the_declared_type(self):
        for name in ("x.svg", "x.html", "x.htm", "x.exe", "x.js", "x.php", "x.sh", "x.bat", "photo.php.png", "photo.png.exe",
                     "a.jpg.html", "x.svgz", "noextension", "x.", ".png"):
            response = self.prepare(self.member, usage="site_report_photo", filename=name, content_type="image/png")
            self.assertEqual(response.status_code, 400, name)
        # an extension of another usage is refused too (a document is not a site report photo)
        docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        self.assertEqual(self.prepare(self.member, usage="site_report_photo", filename="a.docx", content_type=docx).status_code, 400)
        # ... while the PDF documents the observation form has always accepted stay accepted
        self.assertEqual(self.prepare(self.member, usage="site_report_photo", filename="a.pdf", content_type="application/pdf").status_code, 200)

    def test_11_the_declared_content_type_must_match_the_extension(self):
        for content_type in ("text/html", "image/svg+xml", "application/pdf", "", None, "application/octet-stream"):
            self.assertEqual(self.prepare(self.member, usage="site_report_photo", filename="a.png", content_type=content_type).status_code, 400, content_type)
        self.assertEqual(self.prepare(self.member, usage="site_report_photo", filename="a.jpg", content_type="IMAGE/JPG; charset=x").status_code, 200)

    def test_12_an_excessive_or_invalid_size_is_refused(self):
        limit = policy.USAGES["site_report_photo"].max_bytes
        base = {"usage": "site_report_photo", "filename": "a.png", "content_type": "image/png"}
        self.assertEqual(self.prepare(self.member, size_bytes=limit + 1, **base).status_code, 413)
        self.assertEqual(self.prepare(self.member, size_bytes=limit, **base).status_code, 200)
        for bad in (0, -1, "abc", 1.5, True, None, [1]):
            self.assertEqual(self.prepare(self.member, size_bytes=bad, **base).status_code, 400, bad)

    def test_12b_the_limits_are_configurable_per_environment(self):
        base = {"usage": "site_report_photo", "filename": "a.png", "content_type": "image/png", "size_bytes": 2000}
        with override_settings(SECURE_UPLOAD_MAX_BYTES={"site_report_photo": 1000}):
            self.assertEqual(self.prepare(self.member, **base).status_code, 413)

    def test_13_dangerous_names_are_refused(self):
        for name in ("../a.png", "..\\a.png", "a/b.png", "a\\b.png", "a\x00.png", "a\n.png", "a\u202e.png", ".hidden.png", " a.png",
                     "a.png ", "a:b.png", "x" * 201 + ".png", "", None, 7, "a..png", "c:\\w\\a.png", "%2e%2e/a.png/..",):
            self.assertEqual(self.prepare(self.member, usage="site_report_photo", filename=name, content_type="image/png").status_code, 400, repr(name))


class PrepareSigningTests(UploadBase):
    def prepared(self, **overrides):
        response = self.prepare(self.member, **overrides)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def test_14_16_the_object_name_is_unique_server_built_and_under_an_imposed_prefix(self):
        first, second = self.prepared(), self.prepared()
        self.assertNotEqual(first["fields"]["key"], second["fields"]["key"])
        pending = r"^uploads/pending/test/object_assessment_file/[0-9a-f]{32}\.pdf$"
        final = rf"^uploads/private/test/object_assessment_file/company_{self.company.pk}/project_{self.project.pk}/[0-9a-f]{{32}}\.pdf$"
        for prepared in (first, second):
            self.assertRegex(prepared["fields"]["key"], pending)  # what the browser writes to: pending only
            self.assertRegex(self.ticket(prepared)["key"], final)  # where the server will put it after the checks
            self.assertNotIn("report", prepared["fields"]["key"] + self.ticket(prepared)["key"])  # no client name
            self.assertNotIn("reference", prepared)  # no final key is handed to the browser before finalisation
        self.assertNotEqual(self.ticket(first)["key"], self.ticket(second)["key"])
        self.assertEqual(self.prepare(self.member, filename="x.pdf").status_code, 200)

    def test_15_17_the_signature_is_short_lived_and_carries_the_imposed_conditions(self):
        prepared = self.prepared()
        _kind, bucket, key, fields, conditions, expires = self.s3.calls[-1]
        self.assertEqual((bucket, key, expires), ("test-bucket", prepared["fields"]["key"], 300))
        self.assertTrue(key.startswith("uploads/pending/"))
        self.assertEqual(fields, {"Content-Type": "application/pdf", "Content-Disposition": "inline"})
        self.assertIn(["content-length-range", 1, len(PDF)], conditions)
        self.assertIn({"Content-Type": "application/pdf"}, conditions)
        self.assertEqual((prepared["method"], prepared["url"]), ("POST", "https://test-bucket.s3.amazonaws.com/"))
        self.assertIn("expires_at", prepared)

    def test_non_displayable_files_are_stored_as_attachments(self):
        self.prepared(filename="sheet.xlsx", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertEqual(self.s3.calls[-1][3]["Content-Disposition"], "attachment")

    def test_18_20_no_secret_no_requested_acl_no_public_url(self):
        data = json.loads(self.prepare(self.member).content)
        data.pop("upload_id")  # a signed, non-secret ticket: random characters, not scanned
        body = json.dumps(data).lower().replace("x-amz-signature", "")
        for forbidden in ("akia", "secret", "secretaccesskey", "aws_secret", "credential", "public-read", "x-amz-acl"):
            self.assertNotIn(forbidden, body, forbidden)
        _kind, _bucket, _key, fields, conditions, _expires = self.s3.calls[-1]
        self.assertNotIn("acl", " ".join(map(str, list(fields) + conditions)).lower())

    def test_19_no_real_aws_client_is_ever_built(self):
        self.prepared()
        self.legacy(self.company_user)
        self.assertTrue(self.s3.calls)  # everything went through the fake
        import boto3
        with self.assertRaises(AssertionError):
            boto3.client("s3")

    def test_a_storage_failure_is_a_generic_503(self):
        with patch(S3, side_effect=RuntimeError("SECRET-PROVIDER-DETAIL arn:aws:iam::0:role/x")):
            response = self.prepare(self.member)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.content.decode())
        self.assertNotIn("arn:", response.content.decode())

    def test_the_policy_is_accepted_by_the_real_boto3_signer_offline_with_dummy_keys(self):
        """Offline only: boto3 signs locally, with obviously fake static keys; no connection is made."""
        import botocore.session
        client = botocore.session.get_session().create_client(
            "s3", region_name="eu-west-3", aws_access_key_id="DUMMYDUMMYDUMMYDUMMY", aws_secret_access_key="dummy-not-a-secret")
        with patch(S3, return_value=client):
            body = json.loads(self.prepare(self.member).content)
        decoded = json.loads(base64.b64decode(body["fields"]["policy"]))
        conditions = decoded["conditions"]
        self.assertIn(["content-length-range", 1, len(PDF)], conditions)
        self.assertIn({"Content-Type": "application/pdf"}, conditions)
        self.assertIn({"key": body["fields"]["key"]}, conditions)
        self.assertIn({"bucket": "test-bucket"}, conditions)
        self.assertNotIn("dummy-not-a-secret", json.dumps(body))
        self.assertTrue(body["fields"]["key"].startswith("uploads/pending/"))


class FinalizeTests(UploadBase):
    def prepared(self, user=None, **overrides):
        response = self.prepare(user or self.member, **overrides)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def test_a_complete_valid_upload_is_finalised_and_the_call_is_repeatable(self):
        prepared = self.prepared()
        self.put_object(prepared)
        for _ in range(2):
            response = self.finalize(self.member, prepared["upload_id"])
            self.assertEqual(response.status_code, 200, response.content)
        body = json.loads(response.content)
        final = self.ticket(prepared)["key"]
        self.assertEqual((body["reference"], body["size_bytes"], body["content_type"]), (final, len(PDF), "application/pdf"))
        self.assertEqual(body["original_name"], "report.pdf")
        self.assertIsNone(body["public_url"])  # a PRIVATE usage never gets a URL
        self.assertIn(final, self.s3.objects)  # copied to its final key
        self.assertNotIn(prepared["fields"]["key"], self.s3.objects)  # the pending object is gone

    def test_an_upload_that_is_not_there_yet_is_a_409(self):
        self.assertEqual(self.finalize(self.member, self.prepared()["upload_id"]).status_code, 409)

    def test_the_ticket_of_somebody_else_is_the_same_refusal_as_a_forged_one(self):
        prepared = self.prepared()
        self.put_object(prepared)
        foreign = self.finalize(self.reader, prepared["upload_id"])
        forged = self.finalize(self.member, prepared["upload_id"][:-3] + "abc")
        self.assertEqual((foreign.status_code, forged.status_code), (400, 400))
        self.assertEqual(foreign.content, forged.content)
        self.assertEqual(self.finalize(self.member, "").status_code, 400)
        self.assertEqual(self.finalize(self.member, None).status_code, 400)

    def test_an_expired_ticket_is_refused(self):
        prepared = self.prepared()
        self.put_object(prepared)
        later = time.time() + policy.TICKET_MAX_AGE_SECONDS + 5
        with patch("django.core.signing.time.time", return_value=later):
            self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 400)

    def test_the_rights_are_checked_again_at_finalisation(self):
        prepared = self.prepared()
        self.put_object(prepared)
        RolePermission.objects.filter(role__in=Role.objects.filter(created_by=self.member), permission__key__startswith="object_assessments").delete()
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 403)

    def test_a_file_that_is_not_what_it_says_is_refused_and_removed(self):
        for data in (b"<html><script>alert(1)</script></html>", b"MZ\x90\x00 program", b""):
            prepared = self.prepared()
            self.put_object(prepared, data=data)
            response = self.finalize(self.member, prepared["upload_id"])
            self.assertEqual(response.status_code, 400, data)
            self.assertNotIn(prepared["fields"]["key"], self.s3.objects)
            self.assertNotIn(self.ticket(prepared)["key"], self.s3.objects)  # nothing was ever copied to the final key

    def test_a_larger_object_than_declared_or_another_stored_type_is_refused(self):
        prepared = self.prepared()
        self.put_object(prepared, data=PDF + b"x" * 50)
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 400)
        prepared = self.prepared()
        self.put_object(prepared, content_type="text/html")
        self.assertEqual(self.finalize(self.member, prepared["upload_id"]).status_code, 400)

    def test_an_image_is_checked_against_its_own_signature(self):
        png = {"usage": "site_report_photo", "filename": "a.png", "content_type": "image/png", "size_bytes": len(PNG)}
        good = self.prepared(**png)
        self.put_object(good, data=PNG, content_type="image/png")
        self.assertEqual(self.finalize(self.member, good["upload_id"]).status_code, 200)
        bad = self.prepared(**png)
        self.put_object(bad, data=PDF, content_type="image/png")
        self.assertEqual(self.finalize(self.member, bad["upload_id"]).status_code, 400)

    def test_the_finalise_request_accepts_the_reference_only(self):
        request = APIRequestFactory().post("/x/", {"upload_id": "x", "key": "uploads/other"}, format="json")
        force_authenticate(request, user=self.member)
        response = FINALIZE(request)
        response.render()
        self.assertEqual(response.status_code, 400)

    def test_the_content_signatures_of_every_format_are_recognised(self):
        cases = {"jpg": b"\xff\xd8\xff\xe0x", "png": PNG, "gif": b"GIF89a..", "webp": b"RIFF\x00\x00\x00\x00WEBPVP8", "pdf": PDF,
                 "docx": b"PK\x03\x04x", "xlsx": b"PK\x03\x04x", "doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1x", "msg": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1x",
                 "rtf": b"{\\rtf1 x", "txt": b"plain text", "csv": b"a,b\n1,2", "json": b'{"a": 1}', "xml": b"<?xml version='1.0'?><a/>"}
        for extension, head in cases.items():
            self.assertTrue(policy.content_matches_extension(extension, head), extension)
        for extension in cases:
            self.assertFalse(policy.content_matches_extension(extension, b"\x00\x01\x02 not that"), extension)


class LegacyEndpointTests(UploadBase):
    def test_21_an_authorised_user_stores_a_valid_file_under_a_server_built_name(self):
        response = self.legacy(self.company_user)
        self.assertEqual(response.status_code, 201, response.content)
        _kind, bucket, key, extra = self.last("upload")
        self.assertEqual(bucket, "test-bucket")
        # a preview helper: it lives under the PENDING prefix (it expires), under a server-built name
        self.assertRegex(key, r"^uploads/pending/test/legacy_multipart/[0-9a-f]{32}\.pdf$")
        self.assertEqual(extra, {"ContentType": "application/pdf", "ContentDisposition": "inline"})
        # `s3_url` is now a link valid five minutes, not a permanent public URL
        self.assertEqual(json.loads(response.content), {"s3_url": f"https://test-bucket.s3.amazonaws.com/{key}?X-FAKE-SIGNATURE=1"})
        self.assertEqual(self.last("presign_get")[3], 300)

    def test_22_a_user_without_the_existing_permission_is_refused(self):
        self.assertEqual(self.legacy(self.reader).status_code, 403)
        self.assertEqual(self.s3.calls, [])

    def test_22b_anonymous_and_inactive_licence_are_refused(self):
        self.assertEqual(self.legacy(None).status_code, 401)
        with patch(LICENCE, side_effect=InactiveLicenseException()):
            self.assertEqual(self.legacy(self.company_user).status_code, 423)
        self.assertEqual(self.s3.calls, [])

    def test_23_24_the_pending_object_carries_no_account_and_no_client_name(self):
        other = make_user("up.other@links-local.invalid")
        other.company = self.other_company
        other.save(update_fields=["company"])
        UserRole.objects.create(user=other, role=self.role(other, "company", ["projects.create"]), company=self.other_company, assigned_by=other)
        self.legacy(other)
        self.assertNotRegex(self.last("upload")[2], r"company_|user_|report")

    def test_25_27_forbidden_names_and_formats_are_refused(self):
        for name in ("page.html", "page.htm", "logo.svg", "run.exe", "a.php.pdf", "noext"):
            self.assertEqual(self.legacy(self.company_user, name=name).status_code, 400, repr(name))
        self.assertEqual(self.s3.calls, [])
        # Django already drops any directory part of an uploaded name; whatever is left never reaches the object key
        response = self.legacy(self.company_user, name="../secret-name.pdf")
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("secret-name", self.last("upload")[2])
        self.assertNotIn("..", self.last("upload")[2])

    def test_25b_the_real_bytes_must_match_the_extension(self):
        self.assertEqual(self.legacy(self.company_user, name="fake.pdf", content=b"<html>not a pdf</html>").status_code, 400)
        self.assertEqual(self.legacy(self.company_user, name="fake.png", content=PDF, content_type="image/png").status_code, 400)
        self.assertEqual(self.legacy(self.company_user, name="empty.pdf", content=b"").status_code, 400)

    def test_26_an_oversized_file_is_a_413(self):
        with override_settings(SECURE_UPLOAD_MAX_BYTES={"legacy_multipart": 10}):
            self.assertEqual(self.legacy(self.company_user).status_code, 413)
        self.assertEqual(self.s3.calls, [])

    def test_28_two_uploads_of_the_same_name_never_replace_each_other(self):
        first, second = self.legacy(self.company_user), self.legacy(self.company_user)
        keys = {json.loads(first.content)["s3_url"], json.loads(second.content)["s3_url"]}
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(self.s3.objects), 2)

    def test_29_30_a_storage_error_is_masked_and_not_logged_with_its_detail(self):
        self.s3.fail_with = RuntimeError("SECRET-PROVIDER-DETAIL AccessDenied arn:aws:iam::123:user/x")
        with self.assertLogs("project_management.services.upload_s3", level="WARNING") as logs:
            response = self.legacy(self.company_user)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content.decode().count("SECRET"), 0)
        self.assertNotRegex(" ".join(logs.output), r"SECRET|arn:aws")

    def test_the_historical_response_shape_is_kept(self):
        response = self.legacy(self.company_user, name="photo.png", content=PNG, content_type="image/png")
        self.assertEqual((response.status_code, list(json.loads(response.content))), (201, ["s3_url"]))
        self.assertEqual(self.last("upload")[3]["ContentType"], "image/png")

    def test_a_missing_file_keeps_the_serializer_400(self):
        request = APIRequestFactory().post("/x/", {}, format="multipart")
        force_authenticate(request, user=self.company_user)
        response = LEGACY(request)
        response.render()
        self.assertEqual(response.status_code, 400)


class NoCredentialInTheCodeTests(UploadBase):
    def test_the_new_modules_and_the_endpoint_never_read_or_hardcode_a_credential(self):
        import inspect
        from project_management.services import upload_s3
        from project_management.views import project as project_views, secure_upload
        sources = [inspect.getsource(m) for m in (upload_s3, secure_upload, policy)]
        legacy = inspect.getsource(ProjectViewSet.upload_file_to_s3)
        for text in sources + [legacy]:
            self.assertNotRegex(text, r"AKIA[A-Z0-9]{16}")
            self.assertNotRegex(text, r"aws_access_key_id|aws_secret_access_key|AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID")
            self.assertNotRegex(text, r"print\(")
        self.assertNotIn("boto3", inspect.getsource(project_views).replace("# ", ""))
        self.assertIn("get_s3_client", inspect.getsource(upload_s3))
        self.assertIsNotNone(re.search(r"boto3\.client\(\"s3\", region_name=", inspect.getsource(upload_s3)))


class NothingIsPublishedTests(UploadBase):
    def test_20_no_media_file_no_public_url_and_no_object_acl_is_ever_created(self):
        import os
        import shutil
        import tempfile
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        with override_settings(MEDIA_ROOT=media):
            prepared = self.prepare(self.member)
            body = json.loads(prepared.content)
            self.put_object(body)
            finalized = self.finalize(self.member, body["upload_id"])
            legacy = self.legacy(self.company_user)
        self.assertEqual((prepared.status_code, finalized.status_code, legacy.status_code), (200, 200, 201))
        self.assertEqual(os.listdir(media), [])  # nothing written to the (public) media area
        for response in (prepared, finalized):
            self.assertNotIn("/media/", response.content.decode())
        for call in self.s3.calls:  # no ACL is requested anywhere
            self.assertNotIn("acl", json.dumps(call, default=str).lower())
