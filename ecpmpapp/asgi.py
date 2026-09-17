"""
ASGI config for ecpmpapp project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os

import django
from channels.routing import ProtocolTypeRouter, URLRouter
from chat.consumer import ChatConsumer
from django.core.asgi import get_asgi_application
from django.urls import path
from ecpmpapp.jwt_token_middleware import WebSocketBlockedTokenMiddleware
from notifications.consumers import NotificationConsumer

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ecpmpapp.settings.development")


django.setup()

django_http_application = get_asgi_application()


async def http_application(scope, receive, send):
    """Keep process health checks independent from Django request handling."""
    if scope.get("path") in {"/health/live/", "/health/ready/"}:
        body = b"ok"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
        return
    await django_http_application(scope, receive, send)


application = ProtocolTypeRouter(
    {
        "http": http_application,
        "websocket": WebSocketBlockedTokenMiddleware(
            URLRouter(
                [
                    path("api/ws/chat/<int:room_id>/", ChatConsumer.as_asgi()),
                    path("api/ws/notifications/", NotificationConsumer.as_asgi()),
                ]
            )
        ),
    }
)
