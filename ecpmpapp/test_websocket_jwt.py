"""LINKS-RECETTE-05AK: the WebSocket middleware must validate Auth0 JWTs exactly like the HTTP API
(SimpleJWT: RS256 signature against the JWKS, issuer, audience, expiry). Throw-away RSA keys are
generated here; the JWKS lookup is replaced by the public key of the "good" key. No real token."""
import asyncio
import time
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.test import TransactionTestCase
from jwt import PyJWKClient

from ecpmpapp.jwt_token_middleware import WebSocketBlockedTokenMiddleware
from models.models import BlockedToken, User

GOOD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SUB = "auth0|ws-test-user"


class _SigningKey:
    def __init__(self, key):
        self.key = key.public_key()


def make_token(key=GOOD_KEY, **claims):
    payload = {
        "iss": settings.SIMPLE_JWT["ISSUER"],
        "aud": settings.SIMPLE_JWT["AUDIENCE"],
        "sub": SUB,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test"})


class WebSocketJwtTests(TransactionTestCase):
    def setUp(self):
        patcher = mock.patch.object(
            PyJWKClient, "get_signing_key_from_jwt", lambda client, token: _SigningKey(GOOD_KEY)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create(email="ws-test@example.invalid", last_name="Test", auth0_sub=SUB)

    def connect(self, token):
        """Run the middleware; returns (close code or None, user seen by the wrapped app or None)."""
        sent, seen = [], {}

        async def app(scope, receive, send):
            seen["user"] = scope["user"]

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
        }
        asyncio.run(WebSocketBlockedTokenMiddleware(app)(scope, None, send))
        close = next((m["code"] for m in sent if m["type"] == "websocket.close"), None)
        return close, seen.get("user")

    def assertRejected(self, token, code=4002):
        close, user = self.connect(token)
        self.assertEqual(close, code)
        self.assertIsNone(user)

    def test_valid_token_is_accepted(self):
        close, user = self.connect(make_token())
        self.assertIsNone(close)
        self.assertEqual(user.pk, self.user.pk)

    def test_invalid_signature_is_refused(self):
        self.assertRejected(make_token(key=OTHER_KEY))

    def test_wrong_audience_is_refused(self):
        self.assertRejected(make_token(aud="https://wrong.example.invalid"))

    def test_wrong_issuer_is_refused(self):
        self.assertRejected(make_token(iss="https://wrong.example.invalid/"))

    def test_expired_token_is_refused(self):
        self.assertRejected(make_token(exp=int(time.time()) - 60))

    def test_unknown_user_is_refused(self):
        self.assertRejected(make_token(sub="auth0|nobody"))

    def test_inactive_user_is_refused(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertRejected(make_token())

    def test_deleted_user_is_refused(self):
        User.objects.filter(pk=self.user.pk).update(is_deleted=True)
        self.assertRejected(make_token())

    def test_blocked_token_is_refused(self):
        token = make_token()
        BlockedToken.objects.create(token=token, user=self.user, is_blocked=True)
        self.assertRejected(token, code=4001)

    def test_new_token_blocks_the_previous_one(self):
        first = make_token(iat=int(time.time()) - 5)
        self.assertIsNone(self.connect(first)[0])
        self.assertIsNone(self.connect(make_token())[0])
        self.assertRejected(first, code=4001)
