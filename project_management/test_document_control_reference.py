import threading
from datetime import date
from contextlib import ExitStack
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TransactionTestCase
from rest_framework.permissions import AllowAny
from rest_framework.test import APIRequestFactory, force_authenticate

from models.configuration_sync import PRESERVE_LOCAL, REPLACE

from models.document_control_configuration import (
    DocumentControlConfigurationError,
    default_document_control_configuration,
    get_document_control_configuration,
    resolve_document_control_configuration,
    validate_document_control_configuration,
)
from models.company_codes import derive_company_code
from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import (
    Document,
    DocumentReferenceCounter,
    ECPMPConfig,
    Folder,
    Project,
)
from project_management.serializers import ECPMPCONFIGSerializer
from project_management.serializers.document import DocumentSerializer
from project_management.services.project_configuration_templates import (
    build_project_configuration_values,
    ensure_project_config_template,
)
from project_management.views.ecpmp_config import ECPMPConfigViewSet
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import perform_action
from project_management.services.company_codes import (
    assert_company_code_change_allowed,
    company_code_is_locked,
)
from project_management.services.project_codes import (
    derive_project_code,
    project_code_is_locked,
    reference_convention_is_frozen,
)
from project_management.test_document_workflow import TWO, WorkflowTestBase

DISCIPLINES = ["Structure:STR", "Electrical:ELE"]
TYPES = ["Drawing:DWG", "Report:REP"]


class ConfigurationTests(WorkflowTestBase):
    def test_missing_or_partial_configuration_resolves_to_the_defaults(self):
        default = default_document_control_configuration()
        self.assertEqual(resolve_document_control_configuration(None), default)
        self.assertEqual(resolve_document_control_configuration({}), default)
        partial = resolve_document_control_configuration(
            {"reference": {"separator": "_"}}
        )
        self.assertEqual(partial["reference"]["separator"], "_")
        self.assertEqual(partial["reference"]["segments"], default["reference"]["segments"])
        self.assertEqual(partial["workflow"], {"default": "THREE_STEP"})
        self.assertEqual(get_document_control_configuration(None), default)
        self.assertEqual(default["reference"]["segments"], [
            "PROJECT", "DISCIPLINE", "DOCUMENT_TYPE", "SEQUENCE"])

    def invalid(self, **reference):
        with self.assertRaises(DocumentControlConfigurationError):
            validate_document_control_configuration({"reference": reference})

    def test_reference_settings_are_validated(self):
        self.invalid(segments=["PROJECT", "NOPE", "SEQUENCE"])  # invalid segment
        self.invalid(segments=["PROJECT", "PROJECT", "SEQUENCE"])  # duplicate
        self.invalid(segments=["PROJECT", "DISCIPLINE"])  # no SEQUENCE
        self.invalid(segments=["SEQUENCE", "SEQUENCE"])  # SEQUENCE twice
        self.invalid(sequence_scope="GLOBAL")
        self.invalid(separator="<>")
        self.invalid(separator="")
        self.invalid(sequence_width=0)
        self.invalid(sequence_width=99)
        self.invalid(sequence_width="5")
        for ok in ({"segments": ["SEQUENCE"]}, {"sequence_scope": "COMPANY"},
                   {"separator": "/", "sequence_width": 3},
                   {"segments": ["COMPANY", "CATEGORY", "SEQUENCE"]}):
            validate_document_control_configuration({"reference": ok})

    def test_revision_and_workflow_settings_are_validated(self):
        validate_document_control_configuration({"revision": {"scheme": "NUMERIC", "start": "0"}})
        validate_document_control_configuration({"revision": {"scheme": "ALPHABETIC", "start": "A"}})
        for bad in ({"scheme": "ROMAN"}, {"scheme": "NUMERIC", "start": "A"},
                    {"scheme": "ALPHABETIC", "start": "1"}, {"start": "a"}):
            with self.assertRaises(DocumentControlConfigurationError):
                validate_document_control_configuration({"revision": bad})
        validate_document_control_configuration({"workflow": {"default": "TWO_STEP"}})
        with self.assertRaises(DocumentControlConfigurationError):
            validate_document_control_configuration({"workflow": {"default": "FOUR_STEP"}})
        with self.assertRaises(DocumentControlConfigurationError):
            validate_document_control_configuration({"unknown": {}})

    def test_a_stored_invalid_object_falls_back_to_the_defaults(self):
        broken = type("C", (), {"document_control_configuration": {"reference": {"segments": []}}})()
        self.assertEqual(
            get_document_control_configuration(broken), default_document_control_configuration()
        )


class ProjectFixtureMixin:
    def make_project(self, name, company=None, user=None, **config):
        company = company or self.document.folder.company_profile
        project = Project.objects.create(
            project_name=name, user=user or self.creator, company_profile=company,
            contract_value=0,
            project_start_date=date(2026, 1, 1),
            project_end_date=date(2026, 12, 31),
        )
        folder = Folder.objects.create(project=project, name=f"{name} docs")
        cfg, _ = ECPMPConfig.objects.update_or_create(
            project=project,
            defaults={
                "document_disciplines": DISCIPLINES,
                "document_types": TYPES,
                "document_control_configuration": config.get("control", {}),
            },
        )
        return project, folder

    def relaxed(self):
        stack = ExitStack()
        for target, kwargs in (
            ("project_management.serializers.document.get_valid_choices",
             {"return_value": ["Drawing", "Report", "Other", "Design"]}),
        ):
            stack.enter_context(patch(target, **kwargs))
        stack.enter_context(patch.object(
            DocumentSerializer, "_get_valid_choices_for_config_key", return_value=[]))
        stack.enter_context(patch.object(DocumentSerializer, "validate_folder", lambda s, v: v))
        stack.enter_context(patch.object(DocumentSerializer, "validate_discipline", lambda s, v: v))
        return stack

    def create_doc(self, folder, discipline="Structure", document_type="Drawing", **extra):
        serializer = DocumentSerializer(
            data={"folder": folder.id, "file": SimpleUploadedFile("d.txt", b"x"),
                  "document_type": document_type, "discipline": discipline, **extra},
            context={"user": self.creator},
        )
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()

    def update_doc(self, document, **data):
        serializer = DocumentSerializer(
            Document.objects.get(pk=document.pk), data=data, partial=True,
            context={"user": self.creator})
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()


class ProjectCodeTests(ProjectFixtureMixin, WorkflowTestBase):
    def test_automatic_proposal_is_simple_and_deterministic(self):
        self.assertEqual(derive_project_code("Marseille Office Renovation"), "MOR")
        self.assertEqual(derive_project_code("Project Alpha"), "PA")
        self.assertEqual(derive_project_code("Alpha"), "ALP")
        self.assertEqual(derive_project_code(""), "PRJ")
        project, _ = self.make_project("Marseille Office Renovation")
        self.assertEqual(project.project_code, "MOR")
        other, _ = self.make_project("Maison Ouverte Rouge")
        self.assertEqual(other.project_code, "MOR2")  # unique within the owner

    def test_code_is_editable_before_the_first_referenced_document(self):
        project, _ = self.make_project("Marseille Office Renovation")
        project.project_code = "MRS"
        project.save()
        self.assertEqual(Project.objects.get(pk=project.pk).project_code, "MRS")

    def test_renaming_the_project_never_changes_the_code(self):
        project, _ = self.make_project("Marseille Office Renovation")
        project.project_name = "Completely Different Name"
        project.save()
        self.assertEqual(Project.objects.get(pk=project.pk).project_code, "MOR")

    def test_code_is_immutable_after_the_first_referenced_document(self):
        project, folder = self.make_project("Marseille Office Renovation")
        project.project_code = "MRS"
        project.save()
        self.assertFalse(project_code_is_locked(project))
        document = self.create_doc(folder)
        self.assertTrue(document.full_document_number.startswith("MRS-"))
        self.assertTrue(project_code_is_locked(project))
        project.project_code = "XYZ"
        with self.assertRaises(ValidationError):
            project.save()
        self.assertEqual(Project.objects.get(pk=project.pk).project_code, "MRS")

    def test_a_project_with_an_existing_reference_is_locked_and_references_are_not_rewritten(self):
        project, folder = self.make_project("Marseille Office Renovation")
        legacy = Document(folder=folder, file_name="old.txt", created_by=self.creator,
                          full_document_number="LEGACY-0001-00")
        legacy.file.save("old.txt", ContentFile(b"x"), save=False)
        legacy.save()
        self.assertTrue(project_code_is_locked(project))
        project.project_code = "NEW"
        with self.assertRaises(ValidationError):
            project.save()
        self.assertEqual(Document.objects.get(pk=legacy.pk).full_document_number, "LEGACY-0001-00")


class ReferenceGenerationTests(ProjectFixtureMixin, WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.project, self.folder = self.make_project("Marseille Office Renovation")
        self.project.project_code = "MRS"
        self.project.save()

    def test_default_reference_has_no_revision_category_or_version(self):
        document = self.create_doc(self.folder, category="Design", revision=None)
        self.assertEqual(document.full_document_number, "MRS-STR-DWG-00001")
        self.assertNotIn("None", document.full_document_number)

    def test_reference_is_permanent_across_versions_approval_and_metadata(self):
        document = self.create_doc(self.folder)
        original = document.full_document_number
        version = add_document_revision(document, ContentFile(b"v", name="f.txt"), self.creator)
        self.assertEqual(Document.objects.get(pk=document.pk).full_document_number, original)
        wf = self.start(TWO, revision=version)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        for change in ({"document_type": "Report"}, {"discipline": "Electrical"},
                       {"category": "Design"}, {"status": "Issued"}, {"revision": 4}):
            self.update_doc(document, **change)
            self.assertEqual(Document.objects.get(pk=document.pk).full_document_number, original)
        # neither a project rename nor a configuration change touches it
        self.project.project_name = "Renamed"
        self.project.save()
        ECPMPConfig.objects.filter(project=self.project).update(
            document_control_configuration={"reference": {"separator": "_"}})
        self.update_doc(document, status="Sent")
        self.assertEqual(Document.objects.get(pk=document.pk).full_document_number, original)

    def test_configured_pattern_separator_and_width_are_used_for_new_documents(self):
        ECPMPConfig.objects.filter(project=self.project).update(
            document_control_configuration={"reference": {
                "segments": ["DOCUMENT_TYPE", "PROJECT", "SEQUENCE"], "separator": "_", "sequence_width": 3}})
        self.assertEqual(self.create_doc(self.folder).full_document_number, "DWG_MRS_001")

    def test_legacy_blank_reference_stays_blank_on_update(self):
        legacy = Document(folder=self.folder, file_name="old.txt", created_by=self.creator)
        legacy.file.save("old.txt", ContentFile(b"x"), save=False)
        legacy.save()
        self.update_doc(legacy, status="Issued")
        self.assertFalse(Document.objects.get(pk=legacy.pk).full_document_number)

    # --- sequence scopes ------------------------------------------------------------------

    def scope(self, scope):
        ECPMPConfig.objects.filter(project=self.project).update(
            document_control_configuration={"reference": {"sequence_scope": scope}})

    def refs(self, folder, *combos):
        return [self.create_doc(folder, d, t).full_document_number for d, t in combos]

    def test_project_scope_increments_across_disciplines_and_types(self):
        self.scope("PROJECT")
        self.assertEqual(
            self.refs(self.folder, ("Structure", "Drawing"), ("Electrical", "Drawing"),
                      ("Structure", "Report")),
            ["MRS-STR-DWG-00001", "MRS-ELE-DWG-00002", "MRS-STR-REP-00003"])

    def test_discipline_scope_restarts_per_discipline(self):
        self.scope("DISCIPLINE")
        self.assertEqual(
            self.refs(self.folder, ("Structure", "Drawing"), ("Structure", "Report"),
                      ("Electrical", "Drawing")),
            ["MRS-STR-DWG-00001", "MRS-STR-REP-00002", "MRS-ELE-DWG-00001"])

    def test_document_type_scope_restarts_per_type(self):
        self.scope("DOCUMENT_TYPE")
        self.assertEqual(
            self.refs(self.folder, ("Structure", "Drawing"), ("Electrical", "Drawing"),
                      ("Structure", "Report")),
            ["MRS-STR-DWG-00001", "MRS-ELE-DWG-00002", "MRS-STR-REP-00001"])

    def test_company_scope_is_continuous_across_projects_of_a_company(self):
        other_project, other_folder = self.make_project("Nice Airport Terminal")
        other_project.project_code = "NAT"
        other_project.save()
        for project in (self.project, other_project):
            ECPMPConfig.objects.filter(project=project).update(
                document_control_configuration={"reference": {"sequence_scope": "COMPANY"}})
        self.assertEqual(self.create_doc(self.folder).full_document_number, "MRS-STR-DWG-00001")
        self.assertEqual(self.create_doc(other_folder).full_document_number, "NAT-STR-DWG-00002")
        self.assertEqual(self.create_doc(self.folder).full_document_number, "MRS-STR-DWG-00003")

    def test_companies_have_independent_company_counters(self):
        other_user = User.objects.create_user(
            email="second.owner@links-local.invalid", first_name="Second", last_name="Owner",
            title="Mr", user_type="company_user")
        other_company = CompanyProfile.objects.create(
            admin_user=other_user, company_name="Second Company", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="x@x.invalid",
            company_tax_reference="T", company_iban="I")
        other_project, other_folder = self.make_project(
            "Second Project", company=other_company, user=other_user)
        for project in (self.project, other_project):
            ECPMPConfig.objects.filter(project=project).update(
                document_control_configuration={"reference": {"sequence_scope": "COMPANY"}})
        self.assertEqual(self.create_doc(self.folder).sequence_number, 1)
        self.assertEqual(self.create_doc(self.folder).sequence_number, 2)
        self.assertEqual(self.create_doc(other_folder).sequence_number, 1)  # own counter

    def test_sequence_width_is_respected(self):
        ECPMPConfig.objects.filter(project=self.project).update(
            document_control_configuration={"reference": {"sequence_width": 3}})
        self.assertEqual(self.create_doc(self.folder).full_document_number, "MRS-STR-DWG-001")

    def test_a_reference_already_in_use_is_never_reused(self):
        taken = Document(folder=self.folder, file_name="t.txt", created_by=self.creator,
                         full_document_number="MRS-STR-DWG-00001")
        taken.file.save("t.txt", ContentFile(b"x"), save=False)
        taken.save()
        # the counter would hand out 1: the collision check skips to the next free number
        DocumentReferenceCounter.objects.filter(project=self.project).delete()
        self.assertEqual(self.create_doc(self.folder).full_document_number, "MRS-STR-DWG-00002")


class FrozenReferenceConventionTests(ProjectFixtureMixin, WorkflowTestBase):
    """Once the first permanent reference exists, the project keeps its reference convention."""

    def setUp(self):
        super().setUp()
        self.project, self.folder = self.make_project(
            "Marseille Office Renovation",
            control={"reference": {"sequence_scope": "PROJECT", "sequence_width": 5}},
        )
        self.config = ECPMPConfig.objects.get(project=self.project)

    def change(self, **sections):
        serializer = ECPMPCONFIGSerializer(
            self.config, data={"document_control_configuration": sections}, partial=True
        )
        if not serializer.is_valid():
            return serializer.errors
        serializer.save()
        self.config.refresh_from_db()
        return None

    def freeze(self):
        doc = self.create_doc(self.folder)
        self.assertTrue(reference_convention_is_frozen(self.project))
        return doc

    def stored(self):
        return get_document_control_configuration(ECPMPConfig.objects.get(project=self.project))

    def call(self, action, data=None):
        request = APIRequestFactory().post("/x/", data or {}, format="json")
        force_authenticate(request, user=self.creator)
        view = ECPMPConfigViewSet.as_view({"post": action})
        with patch.object(ECPMPConfigViewSet, "get_permissions", return_value=[AllowAny()]):
            return view(request, pk=self.config.pk)

    def set_template(self, **reference):
        template = ensure_project_config_template(self.project)
        template.document_control_configuration = {"reference": reference}
        template.save()
        return template

    # 1. before the first permanent reference everything is editable
    def test_reference_configuration_can_change_before_the_first_reference(self):
        self.assertFalse(reference_convention_is_frozen(self.project))
        self.assertIsNone(self.change(reference={
            "segments": ["PROJECT", "DOCUMENT_TYPE", "SEQUENCE"], "separator": "_",
            "sequence_width": 4, "sequence_scope": "DISCIPLINE"}))
        reference = self.stored()["reference"]
        self.assertEqual(reference["separator"], "_")
        self.assertEqual(reference["sequence_scope"], "DISCIPLINE")

    # 2-5. after it, each reference.* setting is frozen
    def assert_frozen(self, **reference):
        self.freeze()
        before = self.stored()
        errors = self.change(reference=reference)
        self.assertIn("document_control_configuration", errors)
        self.assertIn("permanent references", str(errors))
        self.assertEqual(self.stored(), before)

    def test_sequence_scope_is_frozen_after_the_first_reference(self):
        self.assert_frozen(sequence_scope="DISCIPLINE")

    def test_sequence_width_is_frozen_after_the_first_reference(self):
        self.assert_frozen(sequence_width=3)

    def test_separator_is_frozen_after_the_first_reference(self):
        self.assert_frozen(separator="_")

    def test_segments_are_frozen_after_the_first_reference(self):
        self.assert_frozen(segments=["PROJECT", "SEQUENCE"])

    def test_resubmitting_the_same_reference_settings_is_accepted_when_frozen(self):
        self.freeze()
        self.assertIsNone(self.change(reference=self.stored()["reference"]))

    # 6-7. revision and workflow stay editable
    def test_revision_scheme_and_start_remain_editable_when_frozen(self):
        self.freeze()
        self.assertIsNone(self.change(revision={"scheme": "NUMERIC", "start": "1"}))
        self.assertEqual(self.stored()["revision"], {"scheme": "NUMERIC", "start": "1"})

    def test_default_workflow_remains_editable_when_frozen(self):
        self.freeze()
        self.assertIsNone(self.change(workflow={"default": "THREE_STEP"}))
        self.assertEqual(self.stored()["workflow"]["default"], "THREE_STEP")

    # 8. sync / reset cannot overwrite a frozen convention
    def test_sync_and_reset_keep_the_frozen_reference_but_apply_other_values(self):
        template = ensure_project_config_template(self.project)
        template.document_control_configuration = {
            "reference": {"sequence_scope": "DISCIPLINE", "separator": "_", "sequence_width": 3},
            "workflow": {"default": "THREE_STEP"},
        }
        template.save()
        self.freeze()
        frozen = self.stored()["reference"]
        for strategy in (PRESERVE_LOCAL, REPLACE):
            response = self.call("sync_with_admin", {"strategy": strategy})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(self.stored()["reference"], frozen, strategy)
        self.assertEqual(self.stored()["workflow"]["default"], "THREE_STEP")  # not reference: applied
        response = self.call("reset_config")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored()["reference"], frozen)

    def test_sync_and_reset_still_apply_the_template_before_the_first_reference(self):
        template = ensure_project_config_template(self.project)
        template.document_control_configuration = {
            "reference": {"sequence_scope": "DISCIPLINE", "separator": "_"}}
        template.save()
        response = self.call("sync_with_admin", {"strategy": REPLACE})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.stored()["reference"]["sequence_scope"], "DISCIPLINE")
        response = self.call("reset_config")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.stored()["reference"], default_document_control_configuration()["reference"])

    # 9. template changes only reach future projects
    def test_template_changes_do_not_alter_a_started_project_but_apply_to_new_ones(self):
        self.freeze()
        frozen = self.stored()["reference"]
        self.set_template(sequence_scope="DISCIPLINE", separator="_")
        self.assertEqual(self.stored()["reference"], frozen)
        new_project, _ = self.make_project("Another Project")
        values = build_project_configuration_values(new_project)
        reference = resolve_document_control_configuration(
            values["document_control_configuration"])["reference"]
        self.assertEqual(reference["sequence_scope"], "DISCIPLINE")
        self.assertEqual(reference["separator"], "_")
        self.assertFalse(reference_convention_is_frozen(new_project))

    # 10. existing references are untouched
    def test_existing_references_are_unchanged(self):
        first = self.freeze()
        before = Document.objects.get(pk=first.pk).full_document_number
        counter = DocumentReferenceCounter.objects.filter(project=self.project).count()
        self.change(reference={"sequence_scope": "DISCIPLINE"})  # rejected
        self.call("sync_with_admin", {"strategy": REPLACE})
        self.call("reset_config")
        self.assertEqual(Document.objects.get(pk=first.pk).full_document_number, before)
        self.assertEqual(
            DocumentReferenceCounter.objects.filter(project=self.project).count(), counter)
        # reset_config restored document_disciplines to the real canonical (hierarchical)
        # set, which has no flat "Structure:STR" entry; this test is about reference
        # immutability and sequence continuation, not discipline coding, so no discipline is
        # selected here (LOT 2D-12: an unresolvable discipline code now fails cleanly instead
        # of silently guessing one, and that is exercised on its own in
        # CompanyReferenceTests/test_document_control_reference.py).
        second = self.create_doc(self.folder, discipline="")
        self.assertEqual(second.sequence_number, first.sequence_number + 1)


class CompanyFixtureMixin:
    """A company-library folder (no project): COMPANY-DISCIPLINE-DOCUMENT_TYPE-SEQUENCE."""

    def make_company_config(self, folder=None):
        folder = folder or self.document.folder
        CompanyConfig.objects.update_or_create(
            company_profile=folder.company_profile,
            solution="Energy Consultant Solution",
            defaults={
                "document_disciplines": DISCIPLINES,
                "document_types": TYPES,
            },
        )
        return folder

    def relaxed(self):
        stack = ExitStack()
        stack.enter_context(
            patch(
                "project_management.serializers.document.get_valid_choices",
                # "Specification" is an allowed label with no configured code: used to prove
                # the no-fallback failure is a code-resolution error, not a choice rejection.
                return_value=["Drawing", "Report", "Other", "Design", "Specification"],
            )
        )
        stack.enter_context(
            patch.object(DocumentSerializer, "_get_valid_choices_for_config_key", return_value=[])
        )
        stack.enter_context(
            patch.object(DocumentSerializer, "validate_folder", lambda s, v: v)
        )
        stack.enter_context(
            patch.object(DocumentSerializer, "validate_discipline", lambda s, v: v)
        )
        return stack

    def create_company_doc(self, folder, discipline="Structure", document_type="Drawing", **extra):
        serializer = DocumentSerializer(
            data={
                "folder": folder.id,
                "file": SimpleUploadedFile("d.txt", b"x"),
                "document_type": document_type,
                "discipline": discipline,
                **extra,
            },
            context={"user": self.creator},
        )
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()


class CompanyReferenceTests(CompanyFixtureMixin, WorkflowTestBase):
    """COMPANY_CODE-DISCIPLINE_CODE-DOCUMENT_TYPE_CODE-SEQUENCE for a company-library document.

    The shared default reference configuration still literally lists "PROJECT" first (it is
    also used by projects); for a company-library document it resolves to the company's own
    code instead of being left empty (LOT 2D-12).
    """

    def setUp(self):
        super().setUp()
        self.folder = self.make_company_config()
        self.company = self.folder.company_profile
        self.company.company_code = "SHA"
        self.company.save()

    def test_company_document_reference_uses_company_discipline_and_type_codes(self):
        document = self.create_company_doc(self.folder)
        self.assertEqual(document.full_document_number, "SHA-STR-DWG-00001")

    def test_sequence_increments_and_new_codes_are_resolved_per_document(self):
        first = self.create_company_doc(self.folder, "Structure", "Drawing")
        second = self.create_company_doc(self.folder, "Electrical", "Report")
        self.assertEqual(second.sequence_number, first.sequence_number + 1)
        self.assertEqual(second.full_document_number, "SHA-ELE-REP-00002")

    def test_reference_is_permanent_and_never_gains_a_company_code_retroactively(self):
        document = self.create_company_doc(self.folder)
        original = document.full_document_number
        for change in ({"document_type": "Report"}, {"discipline": "Electrical"}, {"status": "Issued"}):
            serializer = DocumentSerializer(
                Document.objects.get(pk=document.pk), data=change, partial=True,
                context={"user": self.creator})
            with self.relaxed():
                self.assertTrue(serializer.is_valid(), serializer.errors)
                serializer.save()
            self.assertEqual(Document.objects.get(pk=document.pk).full_document_number, original)

    # --- no silent fallback: a missing configured code fails cleanly (LOT 2D-12) -----------

    def test_unconfigured_document_type_fails_cleanly_with_no_three_letter_guess(self):
        with self.assertRaises(Exception) as ctx:
            self.create_company_doc(self.folder, document_type="Specification")
        self.assertNotIn("SPE", str(ctx.exception))
        self.assertFalse(Document.objects.filter(full_document_number__icontains="SPE").exists())

    def test_unconfigured_discipline_fails_cleanly_with_no_three_letter_guess(self):
        with self.assertRaises(Exception) as ctx:
            self.create_company_doc(self.folder, discipline="Civil")
        self.assertNotIn("CIV", str(ctx.exception))
        self.assertFalse(Document.objects.filter(full_document_number__icontains="CIV").exists())

    def test_a_failed_creation_does_not_consume_a_sequence_number(self):
        first = self.create_company_doc(self.folder)
        with self.assertRaises(Exception):
            self.create_company_doc(self.folder, document_type="Specification")
        second = self.create_company_doc(self.folder)
        self.assertEqual(second.sequence_number, first.sequence_number + 1)


class CompanyCodeTests(CompanyFixtureMixin, WorkflowTestBase):
    """Company Code: proposed, unique, editable until the first permanent company Document."""

    def test_automatic_proposal_is_simple_and_deterministic(self):
        self.assertEqual(derive_company_code("Sharemundo Links"), "SL")
        self.assertEqual(derive_company_code("Acme Engineering Group"), "AEG")
        self.assertEqual(derive_company_code("Acme"), "ACM")
        self.assertEqual(derive_company_code(""), "CMP")

    def test_a_company_code_is_proposed_automatically_on_creation(self):
        company = self.document.folder.company_profile  # created via CompanyProfile.objects.create
        self.assertTrue(company.company_code)
        self.assertEqual(company.company_code, derive_company_code(company.company_name))

    def test_code_is_unique_across_companies(self):
        company = self.document.folder.company_profile
        other_user = User.objects.create_user(
            email="dup.company@links-local.invalid", first_name="Dup", last_name="Owner",
            title="Mr", user_type="company_user")
        other = CompanyProfile.objects.create(
            admin_user=other_user, company_name=company.company_name,  # same name on purpose
            company_address="1", post_code="0", company_town="T", company_country="C",
            company_tell_no="0", company_mob_no="1", company_web_address="http://x",
            company_email_address="dup@x.invalid", company_tax_reference="T", company_iban="I")
        self.assertNotEqual(other.company_code, company.company_code)
        self.assertTrue(other.company_code.startswith(company.company_code))

    def test_code_is_editable_before_the_first_permanent_company_document(self):
        company = self.document.folder.company_profile
        self.assertFalse(company_code_is_locked(company))
        company.company_code = "SHA"
        company.save()
        self.assertEqual(CompanyProfile.objects.get(pk=company.pk).company_code, "SHA")

    def test_code_is_locked_after_the_first_permanent_company_document(self):
        folder = self.make_company_config()
        company = folder.company_profile
        company.company_code = "SHA"
        company.save()
        self.assertFalse(company_code_is_locked(company))
        document = self.create_company_doc(folder)
        self.assertTrue(document.full_document_number.startswith("SHA-"))
        self.assertTrue(company_code_is_locked(company))
        with self.assertRaises(ValidationError):
            assert_company_code_change_allowed(company, "XYZ")
        # Legacy safety: the already-generated reference is untouched by the refused attempt.
        self.assertEqual(
            Document.objects.get(pk=document.pk).full_document_number,
            document.full_document_number,
        )


class EmptyDocumentTypeConfigRegressionTests(WorkflowTestBase):
    """LOT 2D-12 UI regression: a company/project whose stored `document_types` is empty (the
    real local demo's state, and any company provisioned before the canonical list existed)
    must still let a canonical label (e.g. "Drawing") be selected AND actually create the
    Document -- both the choice validation (`get_valid_choices`) and the reference's code
    resolution (`generate_document_reference`) must honor the same canonical defaults the
    dropdown already shows (include_defaults=true), not just the raw stored list.
    """

    def relaxed(self):
        stack = ExitStack()
        stack.enter_context(patch.object(DocumentSerializer, "validate_folder", lambda s, v: v))
        stack.enter_context(patch.object(DocumentSerializer, "validate_discipline", lambda s, v: v))
        return stack

    def test_get_valid_choices_returns_canonical_company_types_when_stored_is_empty(self):
        from project_management.serializers.time_expense import get_valid_choices

        company = self.document.folder.company_profile
        CompanyConfig.objects.update_or_create(
            company_profile=company, solution="Energy Consultant Solution",
            defaults={"document_types": []},
        )
        choices = {c.split(":")[0] for c in get_valid_choices(
            "document_types", company.id, None, is_company=True
        )}
        self.assertIn("Drawing", choices)

    def test_get_valid_choices_returns_canonical_project_types_when_stored_is_empty(self):
        project, _ = ProjectFixtureMixin.make_project(self, "LOT 2D-12 Regression Project")
        ECPMPConfig.objects.filter(project=project).update(document_types=[])
        from project_management.serializers.time_expense import get_valid_choices

        choices = {c.split(":")[0] for c in get_valid_choices(
            "document_types", project.id, None
        )}
        self.assertIn("Drawing", choices)

    def test_selecting_a_canonical_type_on_an_empty_company_config_creates_the_document(self):
        """Reproduces the exact reported UI bug end-to-end: Register -> New Document ->
        Document Type "Drawing" on a company whose document_types is empty."""
        company = self.document.folder.company_profile
        CompanyConfig.objects.update_or_create(
            company_profile=company, solution="Energy Consultant Solution",
            defaults={"document_types": []},
        )
        serializer = DocumentSerializer(
            data={
                "folder": self.document.folder_id,
                "file": SimpleUploadedFile("ui-regression.txt", b"x"),
                "document_type": "Drawing",
            },
            context={"user": self.creator},
        )
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            document = serializer.save()
        self.assertIn("DRW", document.full_document_number)
        self.assertNotIn("DRA", document.full_document_number)  # no label-guess fallback


class ConcurrencyTests(TransactionTestCase):
    """The counter row is locked while the sequence is allocated (select_for_update)."""

    def test_parallel_allocations_never_return_the_same_sequence(self):
        from project_management.services.document_numbering import allocate_document_sequence

        user = User.objects.create_user(
            email="conc@links-local.invalid", first_name="C", last_name="C", title="Mr",
            user_type="company_user")
        company = CompanyProfile.objects.create(
            admin_user=user, company_name="Conc Company", company_address="1", post_code="0",
            company_town="T", company_country="C", company_tell_no="0", company_mob_no="1",
            company_web_address="http://x", company_email_address="c@x.invalid",
            company_tax_reference="T", company_iban="I")
        folder = Folder.objects.create(company_profile=company, name="Conc folder")
        results, errors = [], []

        def worker():
            try:
                results.append(allocate_document_sequence(folder))
            except Exception as error:  # pragma: no cover
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(1, 7)))
