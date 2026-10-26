"""LOT 3D-3C0 section 9: Correspondence references now follow the SAME structural convention
as the canonical Project Document reference (owner code leads, sequence trails; see
document_reference.py's default PROJECT-DISCIPLINE-DOCUMENT_TYPE-SEQUENCE segment order) --
COR fills the position Document uses for its DOCUMENT_TYPE code, since Correspondence has no
discipline/document_type of its own. Was COR-<code>-00001 (COR leading) under the original
LOT 3B format; changed for NEW references only -- existing stored `reference` values are never
rewritten (no migration touches them, same "permanent once generated" rule Document references
already follow).

Reuses CorrespondenceTestBase's own fixture (project/company/owner, with notifications
silenced) rather than a second, independently-maintained setUp -- test_correspondence.py's own
suite already proves that fixture is sound.

Kept as a small, focused file (rather than folding into test_correspondence.py) so this exact
regression -- and the "existing references are untouched" claim -- has one obvious home.
"""
from project_management.models import Correspondence, CorrespondenceReferenceCounter
from project_management.services.correspondence_reference import generate_correspondence_reference
from project_management.test_correspondence import CorrespondenceTestBase


class CorrespondenceReferenceFormatTests(CorrespondenceTestBase):
    def test_company_scope_reference_is_code_then_COR_then_sequence(self):
        reference = generate_correspondence_reference(company_profile=self.company)
        self.company.refresh_from_db()
        self.assertEqual(reference, f"{self.company.company_code}-COR-00001")

    def test_project_scope_reference_is_code_then_COR_then_sequence(self):
        reference = generate_correspondence_reference(project=self.project)
        self.project.refresh_from_db()
        self.assertEqual(reference, f"{self.project.project_code}-COR-00001")

    def test_freelancer_scope_has_no_leading_code_but_still_uses_COR(self):
        freelancer = self.owner.__class__.objects.create_user(
            email="ref-freelancer@links-local.invalid", first_name="F", last_name="L",
            title="Mr", user_type="freelancer",
        )
        reference = generate_correspondence_reference(freelancer=freelancer)
        self.assertEqual(reference, "COR-00001")

    def test_sequence_still_increments_per_owner_scope_independently(self):
        first = generate_correspondence_reference(project=self.project)
        second = generate_correspondence_reference(project=self.project)
        self.assertTrue(first.endswith("-00001"))
        self.assertTrue(second.endswith("-00002"))
        self.assertEqual(
            CorrespondenceReferenceCounter.objects.get(project=self.project).last_sequence, 2,
        )

    def test_existing_stored_references_are_never_rewritten_by_the_format_change(self):
        # Simulate an "old-format" Correspondence created before this lot (as if generated
        # under LOT 3B's original COR-leading assembly) -- confirms nothing in this codebase
        # regenerates/touches a stored `reference` after creation.
        legacy = Correspondence.objects.create(
            reference=f"COR-{self.company.company_code}-00099",
            direction="OUT",
            status="ISSUED",
            subject="Legacy-format correspondence",
            company_profile=self.company,
            created_by=self.owner,
        )
        legacy.refresh_from_db()
        self.assertEqual(legacy.reference, f"COR-{self.company.company_code}-00099")
