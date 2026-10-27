from django.http import JsonResponse
from django.db import transaction
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken
import urllib.parse
from models.models import BlockedToken, User
from channels.db import database_sync_to_async

class BlockedTokenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            # Check if the token is blocked
            if BlockedToken.objects.filter(token=token, is_blocked=True).exists():
                return JsonResponse({"detail": "User is logged out."}, status=401)

            # try:
            #     # Decode the token to extract user ID (e.g., "sub" claim in Auth0)
            #     decoded_token = jwt_decode(token, options={"verify_signature": False})
            #     user_id = decoded_token.get("sub")
            #     user = User.objects.get(auth0_sub=user_id)
            # except InvalidTokenError:
            #     return JsonResponse({"detail": "Invalid token."}, status=401)
            # except User.DoesNotExist:
            #     return JsonResponse({"detail": "User not found."}, status=401)

            # with transaction.atomic():
            #     user_tokens = BlockedToken.objects.filter(user=user)
            #     active_token = user_tokens.filter(token=token).first()
            #     if not active_token:
            #         # Create or update the current token
            #         BlockedToken.objects.update_or_create(
            #             token=token, defaults={"user": user, "is_blocked": False}
            #         )
            #     user_tokens.exclude(token=token).update(is_blocked=True)

        return self.get_response(request)


class WebSocketBlockedTokenMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            return await self.app(scope, receive, send)

        token = await self.extract_token(scope)
        
        if not token:
            await self.close_connection(send, 4000, "Missing token")
            return

        is_token_blocked = await self.is_token_blocked(token)
        if is_token_blocked:
            await self.close_connection(send, 4001, "Token is blocked")
            return

        try:
            user = await self.authenticate_token(token)
            await self.update_token_status(user, token)
        except Exception as e:
            await self.close_connection(send, 4002, str(e))
            return

        # Add user to scope if authenticated
        scope["user"] = user
        return await self.app(scope, receive, send)

    async def extract_token(self, scope):
        headers = dict(scope.get("headers", {}))
        auth_header = headers.get(b"authorization", b"").decode()
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
            return token
        # If token is not in headers, extract from query string
        query_string = scope.get("query_string", b"").decode()
        query_params = dict(urllib.parse.parse_qsl(query_string))

        token = query_params.get("token")
        if token and token.startswith('Bearer '):
            token = token.split(' ')[1]
            return token
        return None

    @database_sync_to_async
    def is_token_blocked(self, token):
        return BlockedToken.objects.filter(token=token, is_blocked=True).exists()

    @database_sync_to_async
    def authenticate_token(self, token):
        try:
            # Same validation as the HTTP API (SIMPLE_JWT settings): RS256 signature checked against
            # the Auth0 JWKS, issuer, audience and expiry. The "sub" claim is only read afterwards.
            validated_token = AccessToken(token)
            user_id = validated_token["sub"]
            user = User.objects.get(auth0_sub=user_id, is_active=True, is_deleted=False)
            return user
        except User.DoesNotExist:
            raise Exception("User not found")
        except TokenError:
            raise Exception("Invalid token")
        except KeyError:
            raise Exception("Invalid token structure")

    @database_sync_to_async
    def update_token_status(self, user, token):
        with transaction.atomic():
            user_tokens = BlockedToken.objects.filter(user=user)
            active_token = user_tokens.filter(token=token).first()
            if not active_token:
                # Create or update the current token
                BlockedToken.objects.update_or_create(
                    token=token, defaults={"user": user, "is_blocked": False}
                )
            user_tokens.exclude(token=token).update(is_blocked=True)
    async def close_connection(self, send, code, message):
        await send({
            "type": "websocket.close",
            "code": code,
            "reason": message
        })
