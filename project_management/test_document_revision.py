import shutil
import tempfile
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, User
from project_management.models import Document, DocumentRevision, Folder
from project_management.services.document_revision import (
    add_document_revision,
    create_document_revision,
    get_next_versions,
    get_revision_history,
    increment_major,
)
from project_management.views.document_revision import DocumentRevisionViewSet

class NoNotificationsMixin:
    # Notifications read license tables owned by the admin server (absent in the test DB).
    def silence_notifications(self):
        for target in (
            "project_management.services.document_notifications.should_notify_user_comprehensive",
            "ecpmpapp.utils.should_notify_user_comprehensive",
        ):
            patcher = patch(target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)


MEDIA = tempfile.mkdtemp(prefix="links-revision-tests-")


def make_document():
    user = User.objects.create_user(
        email="revision.tester@links-local.invalid",
        first_name="Rev",
        last_name="Tester",
        title="Mr",
        user_type="company_user",
    )
    company = CompanyProfile.objects.create(
        admin_user=user,
        company_name="Revision Test Company",
        company_address="1 Test Street",
        post_code="00000",
        company_town="Testtown",
        company_country="Testland",
        company_tell_no="000",
        company_mob_no="001",
        company_web_address="http://localhost",
        company_email_address="company@links-local.invalid",
        company_tax_reference="TAX",
        company_iban="IBAN",
    )
    folder = Folder.objects.create(company_profile=company, name="Revision folder")
    document = Document(folder=folder, file_name="plan.txt", created_by=user, revision=0)
    document.file.save("plan.txt", ContentFile(b"original"), save=False)
    document.save()
    return user, document


@override_settings(MEDIA_ROOT=MEDIA)
class DocumentRevisionServiceTests(NoNotificationsMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.silence_notifications()
        self.user, self.document = make_document()

    def upload(self, content, name="plan.txt", **kwargs):
        return add_document_revision(
            self.document, ContentFile(content, name=name), self.user, **kwargs
        )

    def read(self, revision):
        with revision.file.open("rb") as handle:
            return handle.read()

    def test_document_without_revision_is_untouched_until_the_first_real_one(self):
        self.assertFalse(DocumentRevision.objects.exists())
        self.assertTrue(self.document.file)

    def test_multiple_versions_belong_to_one_document_and_keep_their_files(self):
        first = self.upload(b"v1")
        second = self.upload(b"v2")
        third = self.upload(b"v3", major_revision="1")

        self.assertEqual(Document.objects.count(), 1)
        labels = [r.label for r in get_revision_history(self.document)]
        # The legacy file is snapshotted once, then A-style versions follow.
        self.assertEqual(sorted(labels), ["0-00", "0-01", "0-02", "1-00"])
        self.assertEqual(
            {r.document_id for r in DocumentRevision.objects.all()},
            {self.document.id},
        )
        self.assertEqual(len({first.file.name, second.file.name, third.file.name}), 3)
        self.assertEqual(self.read(first), b"v1")
        self.assertEqual(self.read(second), b"v2")
        self.assertEqual(self.read(third), b"v3")

    def test_new_version_does_not_overwrite_previous_files_even_with_same_name(self):
        legacy_path = self.document.file.name
        self.upload(b"v1")
        self.upload(b"v2")  # same file name "plan.txt"

        legacy = DocumentRevision.objects.get(minor_revision=0, major_revision="0")
        self.assertEqual(legacy.file.name, legacy_path)
        self.assertEqual(self.read(legacy), b"original")
        paths = list(DocumentRevision.objects.values_list("file", flat=True))
        self.assertEqual(len(paths), len(set(paths)))

    def test_current_document_file_follows_the_latest_version(self):
        latest = self.upload(b"v1")
        latest = self.upload(b"v2")
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, latest.file.name)

    def test_history_is_ordered_newest_first(self):
        self.upload(b"v1")
        self.upload(b"v2")
        history = list(get_revision_history(self.document))
        self.assertEqual(history[0].label, "0-02")
        self.assertEqual(history[-1].label, "0-00")

    def test_a_stored_revision_cannot_be_modified(self):
        revision = self.upload(b"v1")
        revision.file_name = "changed.txt"
        with self.assertRaises(ValidationError):
            revision.save()


@override_settings(MEDIA_ROOT=MEDIA)
class DocumentRevisionApiTests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.user, self.document = make_document()
        self.factory = APIRequestFactory()

    def call(self, action, **kwargs):
        # Default (license) permission classes read admin-server tables absent in the test DB;
        # the access rule under test is the accessible-document lookup.
        view = DocumentRevisionViewSet.as_view({"get": action}, permission_classes=[])
        request = self.factory.get("/x/")
        force_authenticate(request, user=self.user)
        return view(request, **kwargs)

    def test_history_falls_back_to_the_current_file_without_creating_rows(self):
        with patch(
            "project_management.views.document_revision.get_register_document_or_404",
            return_value=self.document,
        ):
            response = self.call("list", document_id=self.document.id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertTrue(response.data["results"][0]["is_current"])
        self.assertIsNone(response.data["results"][0]["id"])
        self.assertFalse(DocumentRevision.objects.exists())

    def test_history_and_download_of_a_stored_revision(self):
        revision = add_document_revision(
            self.document, ContentFile(b"v1", name="plan.txt"), self.user
        )
        with patch(
            "project_management.views.document_revision.get_register_document_or_404",
            return_value=self.document,
        ):
            listing = self.call("list", document_id=self.document.id)
            download = self.call(
                "download", document_id=self.document.id, revision_id=revision.id
            )
        self.assertEqual(listing.data["count"], 2)
        self.assertTrue(listing.data["results"][0]["is_current"])
        self.assertFalse(listing.data["results"][1]["is_current"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"v1")

    def test_inaccessible_document_is_a_404_for_history_and_download(self):
        with patch(
            "project_management.views.document_revision.get_register_document_or_404",
            side_effect=Http404,
        ):
            self.assertEqual(
                self.call("list", document_id=self.document.id).status_code, 404
            )
            self.assertEqual(
                self.call(
                    "download", document_id=self.document.id, revision_id=1
                ).status_code,
                404,
            )

    def test_revision_of_another_document_cannot_be_downloaded(self):
        _user, other = make_document_for_other_tests(self.user)
        foreign = add_document_revision(
            other, ContentFile(b"secret", name="s.txt"), self.user
        )
        with patch(
            "project_management.views.document_revision.get_register_document_or_404",
            return_value=self.document,
        ):
            response = self.call(
                "download", document_id=self.document.id, revision_id=foreign.id
            )
        self.assertEqual(response.status_code, 404)


def make_document_for_other_tests(user):
    folder = Folder.objects.create(
        company_profile=user.company or CompanyProfile.objects.first(),
        name="Other folder",
    )
    document = Document(folder=folder, file_name="other.txt", created_by=user)
    document.file.save("other.txt", ContentFile(b"other"), save=False)
    document.save()
    return user, document


@override_settings(MEDIA_ROOT=MEDIA)
class DocumentRevisionCreationTests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.user, self.document = make_document()
        # Start the document on major revision A (A-00).
        self.a00 = add_document_revision(
            self.document,
            ContentFile(b"a00", name="plan.txt"),
            self.user,
            major_revision="A",
        )

    def create(self, content=b"new", revision_type="minor", comment="Because"):
        file = ContentFile(content, name="plan.txt") if content is not None else None
        return create_document_revision(
            self.document, file, self.user, revision_type, comment
        )

    def test_minor_version_follows_the_current_one(self):
        revision = self.create(b"a01")
        self.assertEqual(revision.label, "A-01")
        self.assertEqual(self.create(b"a02").label, "A-02")

    def test_new_major_revision_restarts_the_minor_version(self):
        self.create(b"a01")
        revision = self.create(b"b00", revision_type="major")
        self.assertEqual(revision.label, "B-00")
        self.assertEqual(self.create(b"b01").label, "B-01")

    def test_next_versions_are_exposed_for_the_ui(self):
        self.assertEqual(
            get_next_versions(self.document), {"minor": "A-01", "major": "B-00"}
        )

    def test_numbering_helpers(self):
        self.assertEqual(increment_major("A"), "B")
        self.assertEqual(increment_major("Z"), "AA")
        self.assertEqual(increment_major("0"), "1")
        with self.assertRaises(ValidationError):
            increment_major("a-1")

    def test_file_comment_and_type_are_required(self):
        for kwargs in (
            {"content": None},
            {"comment": "   "},
            {"comment": None},
            {"revision_type": "other"},
        ):
            with self.assertRaises(ValidationError):
                self.create(**kwargs)
        self.assertEqual(DocumentRevision.objects.filter(document=self.document).count(), 2)

    def test_comment_and_author_are_stored_and_reference_is_unchanged(self):
        reference = self.document.full_document_number
        revision = self.create(b"a01", comment="  Fixed dimensions  ")
        self.assertEqual(revision.comment, "Fixed dimensions")
        self.assertEqual(revision.created_by, self.user)
        self.document.refresh_from_db()
        self.assertEqual(self.document.full_document_number, reference)
        self.assertEqual(self.document.file.name, revision.file.name)

    def test_document_title_is_stable_and_only_the_extension_follows_the_file(self):
        self.document.refresh_from_db()
        title = self.document.file_name  # plan.txt
        self.create(b"a01")
        self.document.refresh_from_db()
        self.assertEqual(self.document.file_name, title)
        create_document_revision(
            self.document,
            ContentFile(b"pdf", name="whatever.pdf"),
            self.user,
            "minor",
            "Now a PDF",
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.file_name, "plan.pdf")

    def test_previous_files_stay_retrievable_and_are_not_overwritten(self):
        self.create(b"a01")
        self.create(b"b00", revision_type="major")
        by_label = {r.label: r for r in get_revision_history(self.document)}
        for label, content in (("A-00", b"a00"), ("A-01", b"a01"), ("B-00", b"b00")):
            with by_label[label].file.open("rb") as handle:
                self.assertEqual(handle.read(), content)

    def test_duplicate_version_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentRevision.objects.create(
                document=self.document,
                major_revision="A",
                minor_revision=0,
                file="documents/x.txt",
            )
        with patch.object(DocumentRevision, "save", side_effect=IntegrityError):
            with self.assertRaises(ValidationError):
                self.create(b"dup")


@override_settings(MEDIA_ROOT=MEDIA)
class DocumentRevisionCreateApiTests(NoNotificationsMixin, TestCase):
    ACCESS = "project_management.views.document_revision.get_register_document_or_404"
    CAN_UPDATE = "project_management.views.document_revision.assert_can_update_document"

    def setUp(self):
        self.silence_notifications()
        self.user, self.document = make_document()

    def post(self, data):
        view = DocumentRevisionViewSet.as_view(
            {"post": "create"}, permission_classes=[]
        )
        request = APIRequestFactory().post("/x/", data, format="multipart")
        force_authenticate(request, user=self.user)
        return view(request, document_id=self.document.id)

    def payload(self, **overrides):
        data = {
            "file": SimpleUploadedFile("plan.txt", b"api"),
            "revision_type": "minor",
            "comment": "API revision",
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def test_creates_a_version_and_returns_it(self):
        with patch(self.ACCESS, return_value=self.document), patch(self.CAN_UPDATE):
            response = self.post(self.payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["label"], "0-01")
        self.assertEqual(response.data["comment"], "API revision")
        self.assertTrue(response.data["file_url"].endswith("plan.txt"))

    def test_missing_file_or_comment_is_a_400(self):
        with patch(self.ACCESS, return_value=self.document), patch(self.CAN_UPDATE):
            no_file = self.post(self.payload(file=None))
            no_comment = self.post(self.payload(comment=""))
        self.assertEqual(no_file.status_code, 400)
        self.assertIn("file", no_file.data)
        self.assertEqual(no_comment.status_code, 400)
        self.assertIn("comment", no_comment.data)
        self.assertFalse(DocumentRevision.objects.exists())

    def test_inaccessible_document_is_a_404(self):
        with patch(self.ACCESS, side_effect=Http404):
            self.assertEqual(self.post(self.payload()).status_code, 404)

    def test_user_who_cannot_update_gets_a_403(self):
        with patch(self.ACCESS, return_value=self.document), patch(
            self.CAN_UPDATE, side_effect=PermissionDenied
        ):
            self.assertEqual(self.post(self.payload()).status_code, 403)
        self.assertFalse(DocumentRevision.objects.exists())
