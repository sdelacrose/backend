"""GED-SEC.08.2-B0.3-FIX: the photos of a site report, for its PDF.

Since B0.3 a NEW photo is stored in the observation as the PRIVATE reference of its upload (`uploads/private/...`), not as
a URL: nothing can be fetched from it. The PDF engine (WeasyPrint) loads every `<img src>` through a URL fetcher, so the
template is given opaque tokens (`sr-photo:N`) and this module's fetcher answers them:

  * a PRIVATE reference is authorised BEFORE the rendering (the report's project, a team company or user, the
    environment, the usage; the caller's right on the report), then its bytes are read by the server from its own
    storage (`upload_s3.read_object_bytes`), checked as a real image and handed to the engine. No URL and no link of a
    private file is ever created, and no request is made to S3 on the network;
  * a HISTORICAL value (a URL recorded before B0.3) is used only when it points to the Sharemundo storage: a host of the
    configured bucket (read by key through the server storage) or a pinned historical host (one plain HTTPS GET to that
    host, no redirect, no private address). Any other URL (external host, localhost, private address, metadata address,
    other scheme, malformed) is NOT fetched: the photo is left out of the PDF and the case is logged by category;
  * everything else the template asks for is refused: the engine only gets what the template itself owns (its fonts and
    static header logo) and the exact company logo URL it always loaded.

A photo that is not there, corrupt, too large or not an image is left out (the engine already behaved so for a broken
image) with a log line that names the category only, never a key or a URL. A private reference that is not allowed for
this report stops the generation with a 403, and a storage failure with a 503: nothing is saved in either case.
"""
import ipaddress
import io
import logging
import posixpath
import socket
import urllib.error
import urllib.request
from urllib.parse import unquote, urlsplit

import weasyprint
from django.conf import settings
from PIL import Image
from rest_framework.exceptions import APIException, PermissionDenied

from project_management.services import upload_policy as policy_lib
from project_management.services import upload_s3

logger = logging.getLogger(__name__)

USAGE = "site_report_photo"
TOKEN_PREFIX = "sr-photo:"
# what a NEW photo can be (B0.3 raster formats) and what the engine has always embedded for an OLD one
NEW_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
HISTORICAL_FORMATS = {**NEW_FORMATS, "BMP": "image/bmp", "TIFF": "image/tiff"}
MAX_PIXELS = 40_000_000
# the host the historical upload route wrote to (see the B0 audit); a plain HTTPS GET to this exact host is the only
# network access of the historical path
LEGACY_STORAGE_HOSTS = ("staging-sharemundo.s3.eu-west-3.amazonaws.com",)
FETCH_TIMEOUT_SECONDS = 10
# what the TEMPLATE itself loads (its fonts and its static header logo): not user data, allowed as it always was
TEMPLATE_URL_PREFIXES = ("https://fonts.googleapis.com/", "https://fonts.gstatic.com/")
TEMPLATE_STATIC_URLS = ("https://staging-sharemundo.s3.eu-west-3.amazonaws.com/Logo+Links+White+Background+(1).png",)


class PhotoForbidden(PermissionDenied):
    default_detail = "You do not have permission to use one of the files of this report."


class PhotoStorageUnavailable(APIException):
    status_code = 503
    default_detail = "A file of this report could not be read. Please try again later."
    default_code = "storage_unavailable"


class _Skipped(Exception):
    """A photo that is left out of the PDF; `category` is what gets logged (never a key or a URL)."""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


def _max_bytes():
    return policy_lib.max_bytes_for(policy_lib.USAGES[USAGE])


def _storage_hosts():
    hosts = set()
    custom = getattr(settings, "AWS_S3_CUSTOM_DOMAIN", None)
    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or ""
    region = getattr(settings, "AWS_S3_REGION_NAME", "") or ""
    if custom:
        hosts.add(custom.lower())
    if bucket:
        hosts.add(f"{bucket}.s3.amazonaws.com".lower())
        if region:
            hosts.add(f"{bucket}.s3.{region}.amazonaws.com".lower())
    return hosts


def historical_source(value):
    """('storage', key) for a URL of the configured bucket, ('pinned', url) for a pinned historical host, else None."""
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443) or not host:
            return None
        path = unquote(parts.path)
    except (ValueError, TypeError, AttributeError):
        return None
    if "\x00" in path or "\\" in path or not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
        return None
    key = posixpath.normpath(path).lstrip("/")
    if not key or key != path.lstrip("/"):  # a doubled, trailing or dotted segment is not a plain object key
        return None
    if host in _storage_hosts():
        return "storage", key
    if host in LEGACY_STORAGE_HOSTS:
        return "pinned", f"https://{host}{parts.path}"
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect would lead to another host
        return None


def _fetch_pinned(url, max_bytes):
    """One plain HTTPS GET to a pinned Sharemundo host: no credential, no redirect, no private address."""
    host = urlsplit(url).hostname
    request = urllib.request.Request(url, headers={"User-Agent": "links-site-report-pdf"})
    try:
        for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise _Skipped("unsafe_address")
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise _Skipped("missing")
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise _Skipped("redirect" if 300 <= exc.code < 400 else "missing")
    except (urllib.error.URLError, OSError, ValueError):
        raise _Skipped("unreachable")
    if len(data) > max_bytes:
        raise _Skipped("too_large")
    return data


def _checked_image(data, formats):
    """The mime type of real image bytes of an accepted format, or the reason why it is left out."""
    if len(data) > _max_bytes():
        raise _Skipped("too_large")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format, (width, height) = image.format, image.size
            if width * height > MAX_PIXELS:
                raise _Skipped("too_large")
            image.verify()
    except _Skipped:
        raise
    except Exception:  # noqa: BLE001 - unreadable, truncated, decompression bomb: all one "corrupt"
        raise _Skipped("corrupt")
    if image_format not in formats:
        raise _Skipped("format")
    return formats[image_format]


class SiteReportPhotoResolver:
    def __init__(self, site_report, request_user, *, allowed_urls=()):
        self.site_report = site_report
        self.user = request_user
        self.allowed_urls = {url for url in allowed_urls if url}
        self._items = {}
        self._skipped = []
        self._storage_error = False
        self._owners = None

    # --- before the rendering: classify and authorise, no input/output ---------------------------------------------
    def _team_owners(self):
        if self._owners is None:
            self._owners = policy_lib.project_team_owners(self.site_report.project)
        return self._owners

    def _may_use_the_report(self):
        user = self.user
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        from project_management.permissions import SiteReportingPermission

        perm = SiteReportingPermission("view", "project")
        context = {"project": self.site_report.project}
        return bool(
            perm._check_view_permission_project(user, self.site_report, context)
            or perm._check_update_permission_project(user, self.site_report, context)
            or perm._check_create_permission(user, context)
        )

    def private_reference(self, value):
        """The parsed reference when `value` has the shape of a B0.3 private upload, else None."""
        if not isinstance(value, str) or not value.startswith(f"{policy_lib._root()}/{policy_lib.PRIVATE}/"):
            return None
        return policy_lib.parse_final_reference(value)

    def authorise_private(self, reference):
        """A well-formed reference that is not THIS report's photo, or a caller who may not use the report, is an
        explicit refusal: it is never left out silently."""
        policy = policy_lib.USAGES[USAGE]
        owner_ok = reference.owner in self._team_owners()
        right_usage = policy_lib.reference_belongs_to(reference, policy, owner=reference.owner, project_id=self.site_report.project_id)
        if not (right_usage and owner_ok and self._may_use_the_report()):
            raise PhotoForbidden()

    def register(self, value):
        """The token the template carries for an image, or None when the photo is left out (logged by category)."""
        reference = self.private_reference(value)
        if reference is not None:
            self.authorise_private(reference)
            kind, payload = "private", value
        else:
            source = historical_source(value) if isinstance(value, str) else None
            if source is None:
                self._skipped.append("unsafe_url")
                return None
            kind, payload = source
        token = f"{TOKEN_PREFIX}{len(self._items)}"
        self._items[token] = (kind, payload)
        return token

    def attachment_link(self, value):
        """The link of an attachment (a PDF): a historical URL stays what it was, a private reference has no URL at all
        (it is read in LINKS by the authorised endpoint), and is authorised like a photo."""
        reference = self.private_reference(value)
        if reference is None:
            return value
        self.authorise_private(reference)
        return ""

    # --- during the rendering: the engine asks for the bytes, one image at a time ---------------------------------
    def url_fetcher(self, url, *args, **kwargs):
        if url in self._items:
            return self._load(url)
        if url in self.allowed_urls or url in TEMPLATE_STATIC_URLS or url.startswith(TEMPLATE_URL_PREFIXES):
            return weasyprint.default_url_fetcher(url, *args, **kwargs)
        raise ValueError("resource not allowed")

    def _load(self, token):
        kind, payload = self._items[token]
        try:
            if kind == "private" or kind == "storage":
                data = upload_s3.read_object_bytes(payload, _max_bytes())
                if data is None:
                    raise _Skipped("missing")
            else:
                data = _fetch_pinned(payload, _max_bytes())
            mime = _checked_image(data, NEW_FORMATS if kind == "private" else HISTORICAL_FORMATS)
            return {"string": data, "mime_type": mime}
        except upload_s3.ObjectTooLarge:
            self._skipped.append("too_large")
        except upload_s3.StorageUnavailable:
            self._storage_error = True
            self._skipped.append("storage_error")
        except _Skipped as skipped:
            self._skipped.append(skipped.category)
        raise ValueError(self._skipped[-1])  # the engine logs this word and leaves the image out

    def finish(self):
        """After the rendering: a storage failure cancels the generation, any other skipped photo is logged."""
        for category in self._skipped:
            logger.warning("site report %s: a photo was left out of the PDF (%s)", self.site_report.pk, category)
        if self._storage_error:
            raise PhotoStorageUnavailable()
