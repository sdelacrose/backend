"""GED-SEC.08.2-B0.3-FIX: the PDF of a site report shows the PRIVATE photos stored by B0.3 and keeps the historical ones.

The real generator (`create_pdf_invoice_by_site_report`, WeasyPrint) renders real PDFs of synthetic reports; the PDF is inspected
structurally (image objects, their intrinsic size and order, links), never by OCR. Same isolation as B0.2/B0.3: temporary test
database, temporary MEDIA_ROOT, in-memory fake S3 (`boto3.client` raises), and any HTTP request raises unless a test patches it
for one precise case. All images and reports are synthetic."""
import io
import logging
import re
import shutil
import socket
import tempfile
import urllib.error
import zlib
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Document, Folder, Observation, ProjectTeamMembers, SiteReport
from project_management.serializers.site_reporting import SiteReportSerializer
from project_management.services import site_report_pdf_photos as photos
from project_management.services import upload_policy as policy
from project_management.test_ged_sec082_b02_secure_upload import UploadBase
from project_management.utils import create_pdf_invoice_by_site_report
from project_management.views.site_reporting import SiteReportViewSet

POLICY = policy.USAGES["site_report_photo"]
HTTP = "urllib.request.OpenerDirector.open"
BUCKET_HOST = "test-bucket.s3.amazonaws.com"


def image(fmt="PNG", size=(40, 20), color=(200, 30, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return buffer.getvalue()


def pdf_images(pdf):
    """(width, height) of every image object of the PDF, in the order the engine wrote them."""
    return list(zip((int(v) for v in re.findall(rb"/Width (\d+)", pdf)), (int(v) for v in re.findall(rb"/Height (\d+)", pdf))))


def pdf_text(pdf):
    """The PDF plus the content of its compressed object streams (link annotations live there)."""
    parts = [pdf]
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            parts.append(zlib.decompress(match.group(1)))
        except zlib.error:
            pass
    return b"\n".join(parts)


def pdf_links(pdf):
    return re.findall(rb"/URI \(([^)]*)\)", pdf_text(pdf))


class PdfBase(UploadBase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.addCleanup(shutil.rmtree, self._media, True)
        self.addCleanup(self._override.disable)
        self.http_attempts = []

        def refuse(request, *args, **kwargs):  # no HTTP request ever succeeds here; every attempt is recorded
            self.http_attempts.append(getattr(request, "full_url", request))
            raise AssertionError("an HTTP request was attempted")

        patcher = patch(HTTP, side_effect=refuse)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        root, _ = Folder.objects.get_or_create(name=f"{self.project.id}_root_folder", defaults={"project": self.project})
        Folder.objects.get_or_create(parent=root, name="site_reports", defaults={"project": self.project})
        self.observer_member = ProjectTeamMembers.objects.get(project=self.project, team_member=self.member)
        self.html = []
        original = __import__("project_management.utils", fromlist=["render_to_string"]).render_to_string

        def capture(*args, **kwargs):
            rendered = original(*args, **kwargs)
            self.html.append(rendered)
            return rendered

        patcher = patch("project_management.utils.render_to_string", side_effect=capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- fixtures ----------------------------------------------------------------------------------------------
    def private_key(self, extension="png", project=None, company=None, usage="site_report_photo", env="test", visibility="private"):
        owner = f"company_{(company or self.company).pk}"
        return f"uploads/{visibility}/{env}/{usage}/{owner}/project_{(project or self.project).pk}/{__import__('uuid').uuid4().hex}.{extension}"

    def put(self, data, extension="png", **where):
        key = self.private_key(extension, **where)
        self.s3.objects[key] = (data, "image/png")
        return key

    def report(self, *observations):
        """A report whose observations carry the given `photos` lists (stored as they are: references, URLs, anything)."""
        report = SiteReport.objects.create(project=self.project, observer=self.observer_member, created_by=self.member)
        for index, photo_list in enumerate(observations or ([],)):
            Observation.objects.create(site_report=report, contractor=self.observer_member, observation=f"obs {index}",
                                       role="r", rectification="x", deadline=timezone.now() + timedelta(days=1), photos=list(photo_list))
        return report

    def unexpected_http(self):
        """HTTP attempts other than the TEMPLATE's own fonts and static logo (the engine swallows the refusal)."""
        return [u for u in self.http_attempts if not u.startswith(photos.TEMPLATE_URL_PREFIXES) and u not in photos.TEMPLATE_STATIC_URLS]

    def generate(self, report, user=None):
        document, action = create_pdf_invoice_by_site_report(report, user or self.member)
        self.assertEqual(self.unexpected_http(), [])
        document.file.open("rb")
        try:
            return document, document.file.read()
        finally:
            document.file.close()

    def warnings(self):
        return " ".join(self.logs.output) if getattr(self, "logs", None) else ""


class ResolutionTests(PdfBase):
    def resolver(self, report=None, user=None):
        return photos.SiteReportPhotoResolver(report or self.report(), user or self.member)

    def test_1_a_private_reference_of_this_report_is_accepted_and_read_by_the_server(self):
        key = self.put(image())
        resolver = self.resolver()
        token = resolver.register(key)
        self.assertTrue(token.startswith("sr-photo:"))
        fetched = resolver.url_fetcher(token)
        self.assertEqual(fetched["mime_type"], "image/png")
        self.assertEqual([call for call in self.s3.calls if call[0] == "get"], [("get", key)])  # by the server storage, by key

    def test_2_3_4_5_a_reference_of_another_project_company_environment_or_usage_is_refused(self):
        resolver = self.resolver()
        refused = {
            "project": self.private_key(project=self.other_project),
            "company": self.private_key(company=self.other_company),
            "environment": self.private_key(env="prod"),
            "usage": self.private_key(usage="feedback_attachment"),
        }
        for label, key in refused.items():
            self.s3.objects[key] = (image(), "image/png")
            with self.assertRaises(PermissionDenied, msg=label):
                resolver.register(key)
        self.assertFalse([call for call in self.s3.calls if call[0] == "get"])  # nothing was even read

    def test_5b_a_reference_of_a_company_of_the_team_is_accepted(self):
        self.assertIsNotNone(self.resolver().register(self.private_key(company=self.company)))

    def test_6_a_malformed_reference_is_not_read_and_is_left_out(self):
        resolver = self.resolver()
        for value in ("uploads/private/test/site_report_photo/company_1/x.png", "uploads/private/../../etc/passwd.png", "uploads/private/", "", None, 5,
                      self.private_key(visibility="public")):  # a PUBLIC key is not a private photo of a report
            self.assertIsNone(resolver.register(value), repr(value))
        self.assertFalse([call for call in self.s3.calls if call[0] in ("get", "head")])

    def test_34_a_caller_without_a_right_on_the_report_or_without_a_session_is_refused(self):
        key = self.put(image())
        report = self.report()
        stranger = self.company_user  # same company, no site reporting right at all
        with self.assertRaises(PermissionDenied):
            photos.SiteReportPhotoResolver(report, stranger).register(key)
        with self.assertRaises(PermissionDenied):
            photos.SiteReportPhotoResolver(report, None).register(key)
        self.assertIsNotNone(photos.SiteReportPhotoResolver(report, self.member).register(key))


class HistoricalTests(PdfBase):
    def resolver(self):
        return photos.SiteReportPhotoResolver(self.report(), self.member)

    def test_11_a_url_of_the_configured_bucket_is_read_by_key_through_the_server_storage(self):
        self.s3.objects["abc-photo.png"] = (image(), "image/png")
        resolver = self.resolver()
        token = resolver.register(f"https://{BUCKET_HOST}/abc-photo.png")
        self.assertEqual(resolver.url_fetcher(token)["mime_type"], "image/png")
        self.assertIn(("get", "abc-photo.png"), self.s3.calls)

    def test_11b_a_url_of_the_pinned_historical_host_is_fetched_once_without_credential_or_redirect(self):
        resolver = self.resolver()
        token = resolver.register("https://staging-sharemundo.s3.eu-west-3.amazonaws.com/abc-old.png")
        seen = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return image()

        def opener_open(opener, request, timeout=None):
            seen.update(url=request.full_url, headers={k.lower() for k in request.headers}, timeout=timeout)
            return Response()

        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("52.95.150.1", 443))]), patch(HTTP, new=opener_open):
            self.assertEqual(resolver.url_fetcher(token)["mime_type"], "image/png")
        self.assertEqual(seen["url"], "https://staging-sharemundo.s3.eu-west-3.amazonaws.com/abc-old.png")
        self.assertNotIn("authorization", seen["headers"])
        self.assertEqual(seen["timeout"], photos.FETCH_TIMEOUT_SECONDS)

    def test_11c_a_redirect_a_private_address_and_a_missing_object_are_left_out(self):
        url = "https://staging-sharemundo.s3.eu-west-3.amazonaws.com/abc-old.png"
        for label, getaddr, opened in (
            ("redirect", [(2, 1, 6, "", ("52.95.150.1", 443))], urllib.error.HTTPError(url, 302, "Found", {}, None)),
            ("private address", [(2, 1, 6, "", ("10.0.0.5", 443))], AssertionError("must not connect")),
            ("loopback", [(2, 1, 6, "", ("127.0.0.1", 443))], AssertionError("must not connect")),
            ("metadata", [(2, 1, 6, "", ("169.254.169.254", 443))], AssertionError("must not connect")),
            ("missing", [(2, 1, 6, "", ("52.95.150.1", 443))], urllib.error.HTTPError(url, 404, "Not Found", {}, None)),
            ("unreachable", [(2, 1, 6, "", ("52.95.150.1", 443))], urllib.error.URLError("down")),
        ):
            resolver = self.resolver()
            token = resolver.register(url)
            with patch("socket.getaddrinfo", return_value=getaddr), patch(HTTP, side_effect=opened):
                with self.assertRaises(ValueError, msg=label):
                    resolver.url_fetcher(token)

    def test_12_to_16_anything_that_is_not_the_sharemundo_storage_is_never_fetched(self):
        resolver = self.resolver()
        for value in (
            "https://evil.example/x.png", "http://evil.example/x.png", "http://localhost/x.png", "https://localhost/x.png", "https://127.0.0.1/x.png",
            "https://10.0.0.5/x.png", "https://192.168.1.10/x.png", "https://169.254.169.254/latest/meta-data/x.png", "https://[::1]/x.png",
            "file:///etc/passwd.png", "ftp://example.test/x.png", "//evil.example/x.png", "https://", "https:///x.png", "https://[::1/x.png", "not a url.png",
            f"http://{BUCKET_HOST}/x.png",  # plain HTTP, even on the right host
            f"https://{BUCKET_HOST}.evil.example/x.png", f"https://{BUCKET_HOST}@evil.example/x.png", f"https://evil.example/{BUCKET_HOST}/x.png",
            f"https://{BUCKET_HOST}:8443/x.png", f"https://{BUCKET_HOST}/../x.png", f"https://{BUCKET_HOST}//x.png", f"https://{BUCKET_HOST}/a/../x.png",
            f"https://{BUCKET_HOST}/x%00.png", f"https://{BUCKET_HOST}/%2e%2e/x.png", f"https://{BUCKET_HOST}/dir/",
        ):
            self.assertIsNone(resolver.register(value), value)
        self.assertEqual(self.s3.calls, [])  # nothing read, nothing fetched (the HTTP patch would have raised)

    def test_17_old_and_new_photos_mix_in_one_report(self):
        resolver = self.resolver()
        self.s3.objects["old.png"] = (image(size=(50, 20)), "image/png")
        old = resolver.register(f"https://{BUCKET_HOST}/old.png")
        new = resolver.register(self.put(image(size=(60, 20))))
        self.assertEqual((old, new), ("sr-photo:0", "sr-photo:1"))
        self.assertEqual([resolver.url_fetcher(old)["mime_type"], resolver.url_fetcher(new)["mime_type"]], ["image/png", "image/png"])


class FetcherTests(PdfBase):
    def test_the_engine_only_gets_data_urls_the_company_logo_and_the_tokens_it_was_given(self):
        resolver = photos.SiteReportPhotoResolver(self.report(), self.member, allowed_urls=("https://logo.example/l.png",))
        for url in ("https://evil.example/x.png", "http://localhost/x", "file:///etc/passwd", "sr-photo:99", f"https://{BUCKET_HOST}/uploads/x.png"):
            with self.assertRaises(ValueError, msg=url):
                resolver.url_fetcher(url)
        with self.assertRaises(ValueError):
            resolver.url_fetcher("data:image/png;base64,iVBORw0KGgo=")  # no data: URL either: the photos are tokens
        with patch("weasyprint.default_url_fetcher", return_value={"string": b"ok"}) as default:
            for allowed in ("https://logo.example/l.png", "https://fonts.googleapis.com/css2?family=Poppins", photos.TEMPLATE_STATIC_URLS[0]):
                self.assertEqual(resolver.url_fetcher(allowed), {"string": b"ok"})
        self.assertEqual(default.call_count, 3)

    def test_7_8_9_10_a_missing_corrupt_oversized_or_forbidden_format_image_is_left_out_by_category(self):
        cases = {
            "missing": None,
            "corrupt": image()[:30],
            "format": image("TIFF"),  # a TIFF is a historical format, not one of the B0.3 raster formats
            "not an image": b"%PDF-1.4 not an image",
        }
        for label, data in cases.items():
            report = self.report()
            resolver = photos.SiteReportPhotoResolver(report, self.member)
            key = self.private_key()
            if data is not None:
                self.s3.objects[key] = (data, "image/png")
            token = resolver.register(key)
            with self.assertRaises(ValueError, msg=label):
                resolver.url_fetcher(token)
        noise = io.BytesIO()
        Image.frombytes("RGB", (100, 100), __import__("os").urandom(30000)).save(noise, "PNG")
        big = self.put(noise.getvalue())  # incompressible, far above the 1000 bytes allowed below
        with override_settings(SECURE_UPLOAD_MAX_BYTES={"site_report_photo": 1000}):
            resolver = photos.SiteReportPhotoResolver(self.report(), self.member)
            with self.assertRaises(ValueError):
                resolver.url_fetcher(resolver.register(big))

    def test_a_storage_failure_cancels_the_generation_with_a_generic_503(self):
        resolver = photos.SiteReportPhotoResolver(self.report(), self.member)
        token = resolver.register(self.put(image()))
        self.s3.fail_get = True
        with self.assertRaises(ValueError):
            resolver.url_fetcher(token)
        with self.assertRaises(photos.PhotoStorageUnavailable) as caught:
            resolver.finish()
        self.assertNotIn("SECRET", str(caught.exception.detail))
        self.assertEqual(caught.exception.status_code, 503)


class PdfRenderingTests(PdfBase):
    def test_18_21_one_private_photo_is_really_embedded_with_its_caption(self):
        key = self.put(image(size=(80, 40)))
        document, pdf = self.generate(self.report([key]))
        self.assertTrue(pdf.startswith(b"%PDF") and pdf.rstrip().endswith(b"%%EOF"))
        self.assertEqual(pdf_images(pdf), [(80, 40)])  # one image, at its own pixel size
        self.assertIn("Image 1", self.html[-1])
        self.assertEqual(document.document_type, "Site Report")

    def test_19_20_several_photos_keep_their_order_and_proportions(self):
        keys = [self.put(image(size=(size, 20))) for size in (40, 70, 55, 90)]
        _, pdf = self.generate(self.report(keys))
        self.assertEqual(pdf_images(pdf), [(40, 20), (70, 20), (55, 20), (90, 20)])
        self.assertEqual(re.findall(r"Image (\d)</p>", self.html[-1]), ["1", "2", "3", "4"])

    def test_all_the_raster_formats_of_a_new_photo_are_embedded(self):
        keys = [self.put(image(fmt, size=(40 + i * 10, 20)), extension=ext) for i, (fmt, ext) in enumerate((("JPEG", "jpg"), ("PNG", "png"), ("GIF", "gif"), ("WEBP", "webp")))]
        _, pdf = self.generate(self.report(keys))
        self.assertEqual([w for w, _h in pdf_images(pdf)], [40, 50, 60, 70])

    def test_22_a_report_without_photo_still_generates(self):
        _, pdf = self.generate(self.report([]))
        self.assertEqual(pdf_images(pdf), [])
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_c_d_historical_urls_alone_and_mixed_with_new_references(self):
        self.s3.objects["old-1.png"] = (image(size=(33, 20)), "image/png")
        old = f"https://{BUCKET_HOST}/old-1.png"
        _, only_old = self.generate(self.report([old]))
        self.assertEqual(pdf_images(only_old), [(33, 20)])
        self.s3.objects["old-2.png"] = (image(size=(44, 20)), "image/png")
        new, new_2 = self.put(image(size=(77, 20))), self.put(image(size=(88, 20)))
        _, mixed = self.generate(self.report([old, new], [new_2, f"https://{BUCKET_HOST}/old-2.png"]))
        self.assertEqual(pdf_images(mixed), [(33, 20), (77, 20), (88, 20), (44, 20)])  # observation order, then photo order

    def test_23_a_pdf_attachment_is_not_treated_as_an_image_and_gets_no_permanent_link(self):
        attachment = self.put(b"%PDF-1.4 synthetic", extension="pdf")
        historical = "https://staging-sharemundo.s3.eu-west-3.amazonaws.com/old-attachment.pdf"
        photo = self.put(image(size=(45, 20)))
        _, pdf = self.generate(self.report([photo, attachment, historical]))
        self.assertEqual(pdf_images(pdf), [(45, 20)])  # the PDFs were not loaded as images
        html = self.html[-1]
        self.assertIn("File 2 (available in LINKS)", html)  # the private one: no link at all
        self.assertIn(f'href="{historical}"', html)  # the historical one: the link it always had
        self.assertNotIn(attachment, html)
        self.assertNotIn(("get", attachment), self.s3.calls)  # never even read
        links = pdf_links(pdf)
        self.assertEqual(links, [historical.encode()])

    def test_24_a_missing_private_image_is_left_out_like_a_broken_one_and_the_pdf_is_still_made(self):
        missing = self.private_key()
        present = self.put(image(size=(61, 20)))
        with self.assertLogs("project_management.services.site_report_pdf_photos", level="WARNING") as logs:
            _, pdf = self.generate(self.report([missing, present]))
        self.assertEqual(pdf_images(pdf), [(61, 20)])
        self.assertIn("(missing)", " ".join(logs.output))
        self.assertNotRegex(" ".join(logs.output), r"uploads/|https?://")

    def test_j_an_invalid_image_does_not_break_the_report(self):
        broken = self.put(b"not an image at all")
        good = self.put(image(size=(62, 20)))
        with self.assertLogs("project_management.services.site_report_pdf_photos", level="WARNING") as logs:
            _, pdf = self.generate(self.report([broken, good]))
        self.assertEqual(pdf_images(pdf), [(62, 20)])
        self.assertIn("(corrupt)", " ".join(logs.output))

    def test_25_no_permanent_public_link_and_no_private_key_in_the_pdf_or_its_html(self):
        key = self.put(image())
        _, pdf = self.generate(self.report([key]))
        for text in (self.html[-1].encode(), pdf_text(pdf)):
            self.assertNotIn(b"uploads/", text)
            self.assertNotIn(BUCKET_HOST.encode(), text)  # the configured bucket never appears: no URL of the photo exists
        self.assertEqual(pdf_links(pdf), [])

    def test_g_h_a_reference_of_another_project_or_company_stops_the_generation_and_saves_nothing(self):
        before = Document.objects.count()
        for key in (self.private_key(project=self.other_project), self.private_key(company=self.other_company)):
            self.s3.objects[key] = (image(), "image/png")
            with self.assertRaises(PermissionDenied):
                self.generate(self.report([key]))
        self.assertEqual(Document.objects.count(), before)

    def test_a_storage_failure_during_the_rendering_saves_no_document(self):
        key = self.put(image())
        self.s3.fail_get = True
        before = Document.objects.count()
        with self.assertRaises(photos.PhotoStorageUnavailable):
            self.generate(self.report([key]))
        self.assertEqual(Document.objects.count(), before)

    def test_the_company_logo_keeps_its_behaviour_and_nothing_else_is_downloaded(self):
        self.project.project_image = "https://logo.example/logo.png"
        self.project.save(update_fields=["project_image"])
        def default(url, *args, **kwargs):  # the logo is served; the template's own fonts and static logo are "offline"
            if url == "https://logo.example/logo.png":
                return {"string": image(size=(88, 44)), "mime_type": "image/png"}
            raise ValueError("offline")

        with patch("weasyprint.default_url_fetcher", side_effect=default) as fetched:
            _, pdf = self.generate(self.report([self.put(image(size=(44, 22)))]))
        asked = [call.args[0] for call in fetched.call_args_list]
        self.assertIn("https://logo.example/logo.png", asked)  # the logo keeps loading exactly as before
        self.assertTrue(all(u == "https://logo.example/logo.png" or u in photos.TEMPLATE_STATIC_URLS or u.startswith(photos.TEMPLATE_URL_PREFIXES) for u in asked), asked)
        self.assertEqual(sorted(pdf_images(pdf)), [(44, 22), (88, 44)])


class SecurityTests(PdfBase):
    def test_26_to_31_no_aws_client_no_http_no_bearer_no_secret_in_logs_no_temporary_file(self):
        key = self.put(image())
        created = []
        with patch("tempfile.mkstemp", side_effect=lambda *a, **k: created.append("mkstemp")), \
                patch("tempfile.mkdtemp", side_effect=lambda *a, **k: created.append("mkdtemp")), \
                patch("tempfile.NamedTemporaryFile", side_effect=lambda *a, **k: created.append("named")), \
                self.assertLogs("project_management", level="DEBUG") as logs:
            logging.getLogger("project_management").warning("probe")  # keeps assertLogs satisfied when nothing is skipped
            self.generate(self.report([key, self.private_key()]))  # one present, one missing
        self.assertEqual(created, [])  # no temporary file at all
        joined = " ".join(logs.output)
        self.assertNotRegex(joined, r"AKIA|SECRET|Bearer|uploads/private|amazonaws")
        import boto3
        with self.assertRaises(AssertionError):
            boto3.client("s3")  # the guard of the base class is still in force

    def test_the_historical_path_never_resolves_a_name_outside_the_allow_list(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("no name resolution for a refused URL")):
            resolver = photos.SiteReportPhotoResolver(self.report(), self.member)
            for value in ("https://evil.example/x.png", "https://localhost/x.png", "https://169.254.169.254/x.png"):
                self.assertIsNone(resolver.register(value))


class IntegrationTests(PdfBase):
    def payload(self, photos_list):
        return {
            "project": self.project.pk,
            "observer": self.observer_member.pk,
            "name": "Synthetic report",
            "observations": [{
                "contractor": self.observer_member.pk, "observation": "o", "role": "r", "rectification": "x",
                "deadline": (timezone.now() + timedelta(days=2)).isoformat(), "agreed": True, "photos": photos_list,
            }],
        }

    def save_through_serializer(self, user, photos_list):
        request = APIRequestFactory().post("/x/", {}, format="json")
        request.user = user
        serializer = SiteReportSerializer(data=self.payload(photos_list), context={"request": request})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    def read_report_pdf(self):
        document = Document.objects.get(folder__name="site_reports", folder__parent__name=f"{self.project.id}_root_folder")
        document.file.open("rb")
        try:
            return document.file.read()
        finally:
            document.file.close()

    def test_35_36_39_40_the_existing_business_path_generates_a_readable_pdf_with_a_private_photo_and_attachment(self):
        new = self.put(image(size=(75, 20)))
        attachment = self.put(b"%PDF-1.4 synthetic", extension="pdf")
        report = self.save_through_serializer(self.member, [new, attachment])
        pdf = self.read_report_pdf()
        self.assertTrue(pdf.startswith(b"%PDF") and pdf.rstrip().endswith(b"%%EOF"))
        self.assertEqual(pdf_images(pdf), [(75, 20)])  # the PDF attachment is not an image
        self.assertEqual(report.observations.get().photos, [new, attachment])  # the stored values are untouched
        self.assertEqual(pdf_links(pdf), [])

    def test_38_a_later_update_regenerates_the_pdf_from_old_and_new_photos(self):
        self.s3.objects["old.png"] = (image(size=(35, 20)), "image/png")
        new = self.put(image(size=(75, 20)))
        report = self.report([f"https://{BUCKET_HOST}/old.png", new])
        request = APIRequestFactory().patch("/x/", {}, format="json")
        request.user = self.member
        serializer = SiteReportSerializer(report, data={"name": "renamed"}, partial=True, context={"request": request})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()  # the existing update path: it regenerates the PDF
        self.assertEqual(pdf_images(self.read_report_pdf()), [(35, 20), (75, 20)])

    def test_37_the_business_entry_point_refuses_a_user_without_the_right_and_the_generator_refuses_too(self):
        new = self.put(image())
        request = APIRequestFactory().post("/x/", self.payload([new]), format="json")
        force_authenticate(request, user=self.reader)  # a team member without any site reporting right
        response = SiteReportViewSet.as_view({"post": "create"})(request)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SiteReport.objects.filter(name="Synthetic report").exists())
        with self.assertRaises(PermissionDenied):  # and a caller that reaches the generator without the right is refused there
            photos.SiteReportPhotoResolver(self.report([new]), self.reader).register(new)

    def test_the_business_entry_point_with_an_authorised_user_creates_the_report_and_its_pdf(self):
        new = self.put(image(size=(66, 20)))
        request = APIRequestFactory().post("/x/", self.payload([new]), format="json")
        force_authenticate(request, user=self.member)
        response = SiteReportViewSet.as_view({"post": "create"})(request)
        response.render()
        self.assertEqual(response.status_code, 201, response.content[:300])
        document = Document.objects.get(document_type="Site Report")
        document.file.open("rb")
        pdf = document.file.read()
        document.file.close()
        self.assertEqual(pdf_images(pdf), [(66, 20)])

    def test_the_binding_still_refuses_a_foreign_reference_before_any_generation(self):
        foreign = self.put(image(), project=self.other_project)
        request = APIRequestFactory().post("/x/", {}, format="json")
        request.user = self.member
        serializer = SiteReportSerializer(data=self.payload([foreign]), context={"request": request})
        self.assertFalse(serializer.is_valid())
        self.assertFalse(Document.objects.filter(document_type="Site Report").exists())
