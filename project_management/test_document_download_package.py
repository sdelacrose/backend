import io
import zipfile
from django.core.files.storage import default_storage
from django.test import SimpleTestCase
from project_management.models import Document, DocumentAttachment
from project_management.services.document_download_package import unique_archive_name
from project_management.test_ged_sec082_a22_downloads import DownloadBase
from project_management.test_transmittal_client_return import VIEW
from project_management.views.document_download_package import DocumentDownloadPackageView

PACKAGE = DocumentDownloadPackageView.as_view()


class ArchiveNameTests(SimpleTestCase):
    def test_names_are_safe_and_duplicate_names_never_overwrite_entries(self):
        used = set()
        self.assertEqual(unique_archive_name('../../annex.pdf', used), 'annex.pdf')
        self.assertEqual(unique_archive_name('folder\\ANNEX.PDF', used), 'ANNEX (2).PDF')
        self.assertEqual(unique_archive_name('..', used), 'attachment')


class DocumentDownloadPackageTests(DownloadBase):
    def unpack(self, response):
        self.assertEqual(response.status_code, 200, getattr(response, 'data', None))
        try:
            with zipfile.ZipFile(io.BytesIO(self.body(response))) as bundle:
                self.assertIsNone(bundle.testzip())
                return {name: bundle.read(name) for name in bundle.namelist()}
        finally:
            # RequestFactory does not manage request_finished inside TestCase transactions.
            response.file_to_stream.close()

    def test_viewer_gets_main_and_all_annexes_with_their_original_bytes(self):
        contents = {'calcul.csv.zip': b'PK-synthetic-csv-archive', 'Capsule.aasx': b'\x00synthetic aasx', 'plan.dxf': b'0\nSECTION\n'}
        for name, content in contents.items():
            DocumentAttachment.objects.create(document=self.doc1, file=self._put('documents/sec082/'+name, content), file_name=name, file_size=len(content), uploaded_by=self.manager)
        response = self.get(PACKAGE, self.summary_reader, document_id=self.doc1.pk)
        self.assertEqual(response['Content-Type'], 'application/zip')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Vary'], 'Authorization')
        self.assertIn('.zip', response['Content-Disposition'])
        files = self.unpack(response)
        self.assertEqual(files, {'main.txt': b'synthetic main file', 'annex.pdf': b'%PDF-1.4 synthetic', **contents})
        self.assertNotIn(b'synthetic revision one', files.values())

    def test_other_document_files_are_never_included(self):
        DocumentAttachment.objects.create(document=self.rev2.document, file=self._put('documents/sec082/foreign.txt',b'foreign'), file_name='foreign.txt', uploaded_by=self.manager)
        files = self.unpack(self.get(PACKAGE,self.manager,document_id=self.doc1.pk))
        self.assertNotIn('foreign.txt',files)
        self.assertNotIn(b'foreign',files.values())

    def test_duplicate_and_unsafe_names_keep_every_file(self):
        DocumentAttachment.objects.create(document=self.doc1,file=self._put('documents/sec082/duplicate.txt',b'duplicate'),file_name='../../MAIN.TXT',uploaded_by=self.manager)
        files = self.unpack(self.get(PACKAGE,self.manager,document_id=self.doc1.pk))
        self.assertEqual(files['main.txt'],b'synthetic main file')
        self.assertEqual(files['MAIN (2).TXT'],b'duplicate')
        self.assertTrue(all('/' not in name and '\\' not in name for name in files))

    def test_metadata_only_document_with_annexes_can_download_them(self):
        Document.objects.filter(pk=self.doc1.pk).update(file='')
        files = self.unpack(self.get(PACKAGE,self.summary_reader,document_id=self.doc1.pk))
        self.assertEqual(files,{'annex.pdf':b'%PDF-1.4 synthetic'})

    def test_missing_file_or_empty_document_never_returns_an_incomplete_success(self):
        default_storage.delete(self.attachment.file.name)
        self.assertEqual(self.get(PACKAGE,self.manager,document_id=self.doc1.pk).status_code,404)
        DocumentAttachment.objects.filter(document=self.doc1).delete()
        Document.objects.filter(pk=self.doc1.pk).update(file='')
        self.assertEqual(self.get(PACKAGE,self.manager,document_id=self.doc1.pk).status_code,404)

    def test_the_same_document_access_is_required_and_anonymous_is_rejected(self):
        nobody = self.grant('zip.nobody@links-local.invalid',self.project,['documents.update'])
        stranger = self.grant('zip.stranger@links-local.invalid',self.other_project,[VIEW])
        for user in (nobody,stranger):
            self.assertEqual(self.get(PACKAGE,user,document_id=self.doc1.pk).status_code,404)
        self.assertEqual(self.get(PACKAGE,self.manager,document_id=99999999).status_code,404)
        self.assertIn(self.get(PACKAGE,None,document_id=self.doc1.pk).status_code,(401,403))
