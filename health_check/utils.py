from django.conf import settings
from urllib.parse import urlparse, urlunparse


def override_base_url(original_url):
    """
    Fully replaces scheme + netloc (host + port) with BACKEND_URL,
    preserving the path and query string.
    """
    original_parsed = urlparse(original_url)
    base_parsed = urlparse(settings.BACKEND_URL)

    # Combine base scheme+netloc with original path+params+query+fragment
    return urlunparse((
        base_parsed.scheme,
        base_parsed.netloc,
        original_parsed.path,
        original_parsed.params,
        original_parsed.query,
        original_parsed.fragment
    )) 