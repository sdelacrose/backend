"""Bundle the current controlled file and its annexes; never historical versions or other documents."""
import posixpath
import shutil
import unicodedata
import zipfile
from tempfile import SpooledTemporaryFile

from django.http import Http404
from project_management.models import DocumentAttachment
from project_management.services.document_file_name import current_document_file_name, safe_part


def unique_archive_name(name, used, fallback='attachment'):
    base = safe_part(posixpath.basename(str(name or '').replace('\\', '/'))) or fallback
    if base in ('.', '..'):
        base = fallback
    stem, extension = posixpath.splitext(base)
    candidate = base
    number = 2
    while unicodedata.normalize('NFC', candidate).casefold() in used:
        candidate = f'{stem} ({number}){extension}'
        number += 1
    used.add(unicodedata.normalize('NFC', candidate).casefold())
    return candidate


def build_document_download_package(document):
    """Caller must first resolve the document through the existing access-controlled register.

    Spool larger archives to disk and copy files in chunks. Any missing file aborts the whole archive
    so a successful response always contains all the selected files. The response owns the open spool.
    """
    main_name = current_document_file_name(document) if document.file else None
    archive = SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode='w+b')
    used = set()
    count = 0
    try:
        with zipfile.ZipFile(archive, mode='w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
            def add(stored, name):
                nonlocal count
                if not stored:
                    raise Http404
                entry = unique_archive_name(name, used)
                with stored.open('rb') as source, bundle.open(entry, 'w', force_zip64=True) as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                count += 1

            if document.file:
                add(document.file, main_name)
            for attachment in DocumentAttachment.objects.filter(document=document).order_by('created_at', 'id').iterator():
                add(attachment.file, attachment.file_name or attachment.file.name)
        if not count:
            raise Http404
        if main_name:
            name = posixpath.splitext(main_name)[0]
        else:
            name = document.full_document_number or document.identification_number or posixpath.splitext(document.file_name or '')[0] or f'document-{document.pk}'
        filename = (safe_part(posixpath.basename(str(name).replace('\\', '/'))) or f'document-{document.pk}') + '.zip'
        archive.seek(0)
        return archive, filename
    except (FileNotFoundError, OSError, ValueError):
        archive.close()
        raise Http404
    except BaseException:
        archive.close()
        raise
