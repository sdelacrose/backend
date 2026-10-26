from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from datetime import timedelta
from django.utils import timezone
from urllib.parse import urlencode


from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from models.models import User, UserOutlookCredentials
from project_management.models import Project
from userapis.microsoft_outlook import get_access_token, sync_outlook_events, is_outlook_token_valid
from userapis import mailbox_oauth
from userapis.recette_guard import RecetteMailBlocked, confirm_tenant_with_microsoft
from ecpmpapp.utils import get_frontend_url_for_solution, get_user_active_solution
from ecpmpapp.constants import MS_OAUTH_AUTHORIZE_URL, MS_OUTLOOK_CALENDAR_SCOPES
from project_management.utils import get_product_name_from_request


AI_ASSISTANT_OUTLOOK_CONNECTED_HTML = """
<!doctype html>
<html>
  <head>
    <title>Outlook calendar connected</title>
  </head>
  <body>
    <p>Outlook calendar is connected. You can close this tab and continue in the AI chat.</p>
    <script>
      if (window.opener) {
        window.opener.postMessage({ type: "outlook_calendar_connected" }, "*");
      }
      window.close();
    </script>
  </body>
</html>
"""


def build_outlook_oauth_url(user, solution=None, project_id=None, redirect_to=None):
    solution = solution or get_user_active_solution(user.id)
    state_parts = [str(user.id), solution]
    if project_id or redirect_to:
        state_parts.append(str(project_id or ""))
    if redirect_to:
        state_parts.append(str(redirect_to))

    params = {
        "client_id": settings.MS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.MS_REDIRECT_URI,
        "response_mode": "query",
        "scope": MS_OUTLOOK_CALENDAR_SCOPES,
        "state": ":".join(state_parts),
    }
    return f"{MS_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


class MicrosoftAuthViewSet(viewsets.ViewSet):

    @action(detail=False, methods=['get'])
    def auth(self, request):
        """Redirect user to Microsoft login page with user_id in scope."""
        user = request.user
        solution = get_product_name_from_request(request)
        project_id = request.query_params.get("project")
        redirect_to = request.query_params.get("redirect_to")
        if user.is_anonymous:
            return Response({"error": "Authentication required"}, status=401)

        return Response({
            "oauth_url": build_outlook_oauth_url(
                user,
                solution,
                project_id=project_id,
                redirect_to=redirect_to,
            )
        }, status=200)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def callback(self, request):
        """Handle Microsoft OAuth callback, retrieve user_id from state, and store token."""
        code = request.GET.get("code")
        state = request.GET.get("state")

        # MAIL-1.1C2: a Mail-connect state (built by mailbox_oauth.build_mail_oauth_url) is
        # routed entirely to its own handler here, at the SAME single redirect URI Azure has
        # registered for this app (never a second callback route) -- everything below this
        # block is the pre-existing Calendar/Contacts flow, completely unchanged for a
        # non-"mail:" state.
        if state and state.startswith(f"{mailbox_oauth.STATE_PREFIX}:"):
            error = request.GET.get("error")
            if error:
                # Section 4: consent refused/cancelled -- no code was ever issued, nothing to
                # exchange, no MailboxConnection status change.
                return Response(
                    {"error": request.GET.get("error_description") or error}, status=400
                )
            if not code:
                return Response({"error": "Authorization code missing"}, status=400)
            parts = state.split(":")
            if len(parts) != 2:
                return Response({"error": "Invalid state parameter"}, status=400)
            _, state_token = parts
            try:
                mailbox = mailbox_oauth.handle_mail_oauth_callback(state_token, code)
            except mailbox_oauth.MailOAuthError as error:
                return Response({"error": str(error)}, status=400)
            return Response(
                {
                    "mailbox_id": mailbox.id,
                    "status": mailbox.status,
                    "connection_type": mailbox.connection_type,
                },
                status=200,
            )

        if not code:
            return Response({"error": "Authorization code missing"}, status=400)
        if not state or ":" not in state:
            return Response({"error": "Invalid state parameter"}, status=400)

        state_parts = state.split(":")
        if len(state_parts) < 2:
            return Response({"error": "Invalid state parameter"}, status=400)
        user_id = state_parts[0]
        solution_name = state_parts[1]
        project_id = state_parts[2] if len(state_parts) >= 3 else None
        redirect_to = state_parts[3] if len(state_parts) >= 4 else None

        # Exchange authorization code for tokens
        tokens = get_access_token(code)
        if "access_token" not in tokens:
            return Response({"error": "Failed to retrieve access token"}, status=400)
        try:
            # LINKS-RECETTE-03: Calendar/Contacts tokens too must belong to the recette tenant;
            # nothing is stored and nothing is synced otherwise. A no-op outside recette.
            confirm_tenant_with_microsoft(tokens["access_token"])
        except RecetteMailBlocked as error:
            return Response({"error": str(error)}, status=403)

        # Retrieve user from database
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)

        # Calculate token expiry timestamp
        expires_in_seconds = tokens.get("expires_in", 3600)
        expiry_time = timezone.now() + timedelta(seconds=expires_in_seconds)

        if hasattr(user, "outlook_credentials"):
            user.outlook_credentials.outlook_access_token = tokens["access_token"]
            user.outlook_credentials.outlook_refresh_token = tokens.get("refresh_token")
            user.outlook_credentials.outlook_token_expires = expiry_time
            user.outlook_credentials.save()
        else:
            UserOutlookCredentials.objects.create(
                user=user,
                outlook_access_token=tokens["access_token"],
                outlook_refresh_token=tokens.get("refresh_token"),
                outlook_token_expires=expiry_time,
            )
        project = None
        if project_id:
            try:
                project = Project.objects.get(id=project_id)
            except Project.DoesNotExist:
                return Response({"error": "Project not found"}, status=404)

        sync_result = sync_outlook_events(user, tokens["access_token"], project=project)

        if not sync_result.get("success"):
            return Response(sync_result.get("error"), status=status.HTTP_400_BAD_REQUEST)

        if redirect_to == "ai_assistant":
            return HttpResponse(AI_ASSISTANT_OUTLOOK_CONNECTED_HTML)
        
        frontend_url = get_frontend_url_for_solution(solution_name)
        if redirect_to == "project_calendar":
            redirect_url = f"https://{frontend_url}/project-calendar-management"
        else:
            redirect_url = f"https://{frontend_url}/calendar-management"
        return redirect(redirect_url)

    @action(detail=False, methods=['post'])
    def sync_calendar(self, request):
        """Trigger manual sync of Outlook events."""
        user = request.user
        if not is_outlook_token_valid(user):
            return Response({
                "error": "Invalid or expired Outlook token. Please reconnect your Outlook account."
            }, status=status.HTTP_401_UNAUTHORIZED)


        access_token = user.outlook_credentials.outlook_access_token

        sync_result = sync_outlook_events(user, access_token)

        if not sync_result.get("success"):
            return Response(sync_result.get("error"), status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "message": sync_result.get("message"),
        }, status=status.HTTP_200_OK)
