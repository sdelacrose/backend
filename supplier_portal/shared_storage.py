"""Published supplier evidence has no public media URL and lives outside MEDIA_ROOT."""
import os
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible

@deconstructible
class PrivateSupplierStorage(FileSystemStorage):
    @property
    def base_location(self):
        location = Path(getattr(settings, 'SUPPLIER_SHARED_STORAGE_ROOT', Path(settings.BASE_DIR) / '.private_supplier_shared')).resolve()
        media = getattr(settings, 'MEDIA_ROOT', None)
        if media and location.is_relative_to(Path(media).resolve()):
            raise ImproperlyConfigured('Supplier shared files must be stored outside the public media directory.')
        return str(location)
    @property
    def location(self):
        return os.path.abspath(self.base_location)
    def url(self, name):
        raise ValueError('Private supplier evidence must be downloaded through its authorized portal endpoint.')
