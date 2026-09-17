from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from ecpmpapp.asgi import http_application


class ASGIHealthEndpointTests(SimpleTestCase):
    def _request_health_endpoint(self, path):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        async_to_sync(http_application)(
            {"type": "http", "method": "GET", "path": path, "headers": []},
            receive,
            send,
        )
        return messages

    def test_live_health_response_bypasses_django(self):
        messages = self._request_health_endpoint("/health/live/")

        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(messages[1]["body"], b"ok")

    def test_ready_health_response_bypasses_django(self):
        messages = self._request_health_endpoint("/health/ready/")

        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(messages[1]["body"], b"ok")
