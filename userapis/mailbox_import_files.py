"""MAIL-1.3D-C2-B3: the safety checks on a file about to be kept from an Outlook message.

What exists in LINKS today: nothing -- the correspondence attachment upload accepts any name, type
or size, and files are only ever served `as_attachment` (never rendered inline), which stays so.
This module adds, for IMPORTED files only (uploads by hand are untouched):

  - a cleaned, path-free file name (control and direction-override characters removed, trailing
    dots and spaces dropped, bounded length); a name carrying a path or a ".." is refused;
  - a deny-list of ACTIVE or dangerous formats, by extension, by declared MIME type, and by the
    first bytes of what was really downloaded (an executable renamed `.pdf` is still refused);
  - the minimal structure of an original message (.eml): MIME headers that make it a real message.

There is NO antivirus here and none is added without a decision: Exchange Online's own filtering is
the only malware control before a file reaches LINKS. A production deployment that keeps these files
should put a scanner (for instance ClamAV, or a storage-level scanner) in front of the storage; until
then these checks reduce, they do not remove, the risk. Nothing is ever executed or rendered.
"""

import email.parser
import os
import re

from django.conf import settings

# Active content, executables, scripts, installers, shortcuts and macro-enabled Office files.
BLOCKED_EXTENSIONS = frozenset({
    "exe", "com", "scr", "bat", "cmd", "msi", "msp", "mst", "dll", "sys", "drv", "ocx", "cpl", "jar",
    "js", "jse", "vbs", "vbe", "wsf", "wsh", "ps1", "psm1", "ps1xml", "psc1", "sh", "bash", "csh",
    "lnk", "url", "hta", "reg", "inf", "pif", "gadget", "application", "appx", "msix", "apk", "bin",
    "docm", "dotm", "xlsm", "xltm", "xlam", "xlsb", "pptm", "potm", "ppam", "ppsm", "sldm",
    # rendered by a browser if opened inline: kept out as a matter of principle
    "html", "htm", "xhtml", "shtml", "svg", "mht", "mhtml", "swf",
})
BLOCKED_CONTENT_TYPES = frozenset({
    "application/x-msdownload", "application/x-dosexec", "application/x-msdos-program",
    "application/x-executable", "application/vnd.microsoft.portable-executable",
    "application/x-sh", "application/x-csh", "application/x-bat", "application/x-msi",
    "application/javascript", "text/javascript", "application/x-javascript",
    "application/java-archive", "application/x-ms-shortcut", "application/hta",
    "text/html", "application/xhtml+xml", "image/svg+xml", "application/x-shockwave-flash",
})
# The first bytes of programs, whatever the file is called or claims to be.
EXECUTABLE_SIGNATURES = (
    b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
)
MAX_NAME_LENGTH = 200
_CONTROL_AND_BIDI = re.compile("[\x00-\x1f\x7f‎‏‪-‮⁦-⁩]")


class FileRefused(Exception):
    """A file that must not be kept; `code` is stable (the caller words it)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class InvalidEml(Exception):
    pass


def clean_file_name(name, fallback="attachment"):
    """A safe, path-free file name, or FileRefused("unsafe_file_name") for a path."""
    raw = str(name or "")
    parts = re.split(r"[\\/]", raw)
    if len(parts) > 1 or ".." in parts or re.match(r"^[A-Za-z]:", raw):
        raise FileRefused("unsafe_file_name")
    cleaned = _CONTROL_AND_BIDI.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")      # Windows ignores trailing dots/spaces
    if cleaned in ("", ".", ".."):
        cleaned = fallback
    stem, extension = os.path.splitext(cleaned)
    if len(cleaned) > MAX_NAME_LENGTH:
        cleaned = stem[: MAX_NAME_LENGTH - len(extension)] + extension
    return cleaned


def _blocked_extensions():
    extra = getattr(settings, "MAIL_IMPORT_EXTRA_BLOCKED_EXTENSIONS", ())
    return BLOCKED_EXTENSIONS | {str(e).lower().lstrip(".") for e in extra}


def check_name_and_type(name, content_type):
    """Before any download: the cleaned name and the declared type must be acceptable."""
    cleaned = clean_file_name(name)
    extension = os.path.splitext(cleaned)[1].lower().lstrip(".")
    if extension in _blocked_extensions():
        raise FileRefused("blocked_extension")
    declared = str(content_type or "").split(";")[0].strip().lower()
    if declared in BLOCKED_CONTENT_TYPES:
        raise FileRefused("blocked_content_type")
    return cleaned


def check_content(fh, size):
    """After the download: not empty, and not a program in disguise. Leaves `fh` at the start."""
    if size <= 0:
        raise FileRefused("empty_file")
    fh.seek(0)
    head = fh.read(8)
    fh.seek(0)
    if any(head.startswith(signature) for signature in EXECUTABLE_SIGNATURES):
        raise FileRefused("blocked_content")


def validate_eml(fh):
    """The minimal structure of a real message: no binary junk up front and the headers of a mail.
    Leaves `fh` at the start. Raises InvalidEml."""
    fh.seek(0)
    head = fh.read(65536)
    fh.seek(0)
    if not head or b"\x00" in head[:4096]:
        raise InvalidEml()
    names = {key.lower() for key in email.parser.BytesHeaderParser().parsebytes(head).keys()}
    if "from" not in names or not ({"date", "message-id"} & names):
        raise InvalidEml()


def eml_file_name(subject):
    """`<subject>.eml`: the subject made safe, a plain fallback when there is none."""
    base = re.sub(r'[\\/:*?"<>|]', "_", str(subject or "")).strip()[:80]
    return clean_file_name(f"{base or 'message'}.eml", fallback="message.eml")
