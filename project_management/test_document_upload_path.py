from types import SimpleNamespace

from django.test import SimpleTestCase

from project_management.models.document import get_upload_path


class DocumentUploadPathTests(SimpleTestCase):
    def _document(self, **folder_owners):
        folder_data = {
            "id": 73,
            "project": None,
            "project_id": None,
            "company_profile": None,
            "company_profile_id": None,
            "freelancer": None,
            "freelancer_id": None,
        }
        folder_data.update(folder_owners)
        folder = SimpleNamespace(**folder_data)
        return SimpleNamespace(folder=folder, folder_id=folder.id)

    def test_project_path_uses_project_id(self):
        document = self._document(
            project=SimpleNamespace(id=41),
            project_id=41,
        )

        path = get_upload_path(document, "drawing.pdf")

        self.assertEqual(
            path,
            "documents/project_41/folder_73/drawing.pdf",
        )

    def test_company_path_uses_company_profile_id(self):
        document = self._document(
            company_profile=SimpleNamespace(id=52),
            company_profile_id=52,
        )

        path = get_upload_path(document, "contract.pdf")

        self.assertEqual(
            path,
            "documents/company_52/folder_73/contract.pdf",
        )

    def test_freelancer_path_uses_freelancer_id(self):
        document = self._document(
            freelancer=SimpleNamespace(id=63),
            freelancer_id=63,
        )

        path = get_upload_path(document, "report.pdf")

        self.assertEqual(
            path,
            "documents/freelancer_63/folder_73/report.pdf",
        )
