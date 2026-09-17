"""Bounded retries for read-only AI proposals, never for applying project data."""

import logging
import time

import requests

logger = logging.getLogger(__name__)


def post_extraction(url, *, headers, json, timeout):
    # These endpoints only generate proposals. Application remains a separate,
    # transactional action, so repeating a request cannot duplicate project data.
    for attempt in range(2):
        try:
            response = requests.post(
                url, headers=headers, json=json, timeout=(10, timeout),
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            logger.warning(
                "AI extraction transport failure: type=%s attempt=%s",
                type(exc).__name__, attempt + 1,
            )
            if attempt == 1:
                raise
        else:
            if response.status_code not in {429, 502, 503, 504} or attempt == 1:
                return response
            logger.warning(
                "AI extraction transient response: status=%s attempt=%s",
                response.status_code, attempt + 1,
            )
        time.sleep(2)
