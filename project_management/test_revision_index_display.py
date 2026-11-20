"""SURVEILLANCE LOT 4: the revision payload exposes the PREL/BPE state and the index a technical BPE version inherits."""
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.services import client_surveillance_followup as fu_service
from project_management.test_client_surveillance_lot2 import Lot2Base
from project_management.views.document_revision import DocumentRevisionViewSet


class RevisionIndexDisplayTests(Lot2Base):
    def rows(self):
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=self.updater)
        response = DocumentRevisionViewSet.as_view({"get": "list"})(request, document_id=self.document.pk)
        self.assertEqual(response.status_code, 200)
        data = response.data
        return {row["id"]: row for row in (data["results"] if isinstance(data, dict) and "results" in data else data)}

    def test_a_technical_bpe_version_shows_the_inherited_index_and_ordinary_versions_are_unchanged(self):
        record = self.ret("ACCEPTED")
        followup = self.confirm(record)
        technical = self.provide_bpe(followup)
        rows = self.rows()
        self.assertEqual((rows[technical.pk]["revision_label"], rows[technical.pk]["index_label"], rows[technical.pk]["issue_state"],
                          rows[technical.pk]["index_revision_id"]), (None, self.revision.revision_label, "BPE", self.revision.pk))
        self.assertEqual((rows[self.revision.pk]["index_label"], rows[self.revision.pk]["index_revision_id"]), (self.revision.revision_label, None))
