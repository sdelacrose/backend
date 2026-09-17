import uuid
import requests
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.exceptions import PermissionDenied
from django.utils.timezone import now
from django.utils.decorators import method_decorator
from project_management.models.constants import COLLABORATIVE_SOLUTION
from project_management.utils import get_product_name_from_request
from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from urllib.parse import urlencode

from models.models import CompanyProfile, User
from project_management.models import AzureToken, OAuthState, Project, Folder
from project_management.serializers import SharePointSiteSYNCSerializer
from project_management.tasks import fetch_sharepoint_data
from project_management.utils import has_project_permission
from project_management.permissions import SharePointProjectPermission
from ecpmpapp.utils import require_any_permission
# from companyprofileapis.decorators import api_login_required
from ecpmpapp.constants import (
    MS_OAUTH_AUTHORIZE_URL, 
    MS_OAUTH_TOKEN_URL, 
    MS_GRAPH_SITES_SEARCH_URL, 
    MS_GRAPH_DRIVE_CHILDREN_URL,
    MS_OAUTH_TENANT_TOKEN_URL
)


#@method_decorator(api_login_required, name="dispatch")
class AzureLoginAPIView(APIView):
    def get(self, request):
        user = request.user
        redirect_uri = request.GET.get('redirect_uri')
        if not user.is_authenticated:
            return Response({"error": "User not authenticated"}, status=401)

        if not redirect_uri:
            return Response({"error": "Redirect uri is required"}, status=400)


        # Generate a unique state parameter to prevent CSRF attacks
        state = str(uuid.uuid4())

        # Store the state in the database or cache with the associated user
        OAuthState.objects.update_or_create(user=user, defaults={"state": state})

        # Construct the Azure login URL
        params = {
            'client_id': settings.AZURE_CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': settings.AZURE_REDIRECT_URI,
            'response_mode': 'query',
            'scope': 'Sites.Read.All Files.ReadWrite.All offline_access',
            'state': state + "$" + redirect_uri
        }
        auth_url = f"{MS_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
        
        return Response(auth_url)

class AzureCallbackAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):

        state = request.GET.get('state')
        code = request.GET.get('code')
        fe_url = request.GET.get('fe_base_url')

        if not state or not code:
            return Response({"error": "State or code missing"}, status=status.HTTP_400_BAD_REQUEST)

        state_list = state.split("$")
        # Retrieve the stored state from the database
        try:
            oauth_state = OAuthState.objects.get(state=state_list[0])
        except OAuthState.DoesNotExist:
            return Response({"error": "Invalid state parameter"}, status=status.HTTP_400_BAD_REQUEST)

        # Validate the state parameter
        if not oauth_state.user.is_authenticated:
            return Response({"error": "User not authenticated"}, status=status.HTTP_401_UNAUTHORIZED)

        # Proceed with token exchange
        token_url = MS_OAUTH_TOKEN_URL
        token_data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': settings.AZURE_REDIRECT_URI,
            'client_id': settings.AZURE_CLIENT_ID,
            'client_secret': settings.AZURE_CLIENT_SECRET,
            'scope': 'Sites.Read.All Files.ReadWrite.All offline_access'
        }

        token_response = requests.post(token_url, data=token_data)

        if token_response.status_code != 200:
            return Response({"error": "Failed to get tokens from Azure"}, status=status.HTTP_400_BAD_REQUEST)

        token_info = token_response.json()

        # Save tokens to the database (specific to the user)
        azure_token, created = AzureToken.objects.update_or_create(
            user=oauth_state.user, 
            defaults={
                "access_token": token_info.get("access_token"),
                "refresh_token": token_info.get("refresh_token"),
                "expires_at": now() + timedelta(seconds=token_info.get("expires_in"))
            }
        )

        # Clean up the OAuth state after successful callback processing
        oauth_state.delete()
        
        return HttpResponseRedirect(state_list[1])


#@method_decorator(api_login_required, name="dispatch")
class FetchSharePointSitesAPIView(APIView):

    def get(self, request):
        try:
            user = request.user
            try:
                azure_token = AzureToken.objects.get(user=user)
            except AzureToken.DoesNotExist:
                return Response({"error": "User not authenticated with Azure"}, status=400)

            # Use the token to request SharePoint sites
            sites_url = MS_GRAPH_SITES_SEARCH_URL
            headers = {
                "Authorization": f"Bearer {azure_token.access_token}"
            }

            response = requests.get(sites_url, headers=headers)
            if response.status_code != 200:
                            # Check for specific error messages and return meaningful responses
                try:
                    error_data = response.json()
                    if 'error' in error_data:
                        error_code = error_data['error'].get('code', '')
                        error_message = error_data['error'].get('message', '')
                        
                        # Handle MSA account error specifically
                        if error_code == 'BadRequest' and 'This API is not supported for MSA accounts' in error_message:
                            return Response({
                                "error": "SharePoint is not accessible with a personal Microsoft account. Please use a work or school account to access SharePoint sites."
                            }, status=status.HTTP_400_BAD_REQUEST)
                        
                        # Return the specific error message from Microsoft Graph API
                        return Response({
                            "error": f"SharePoint API Error: {error_message}"
                        }, status=status.HTTP_400_BAD_REQUEST)
                except (ValueError, KeyError):
                    raise Exception("Failed to fetch SharePoint sites")

            sites_info = response.json()
        except Exception as e:
            return Response({"error": "Something went wrong, Failed to fetch SharePoint sites"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(sites_info.get('value', []))




#@method_decorator(api_login_required, name="dispatch")
class FetchSharePointFilesAPIView(GenericAPIView):
    serializer_class = SharePointSiteSYNCSerializer

    def post(self, request, *args, **kwargs):
        # Check permissions first
        project_id = request.data.get('project_id')
        company_id = request.data.get('company_id')
        
        # Default to company scope, but can be overridden based on context
        scope = 'company'
        if project_id:
            scope = 'project'
        
        # Check if user has the sync_sharepoint permission
        has_permission = require_any_permission(
            request.user, 
            'documents', 
            ['sync_sharepoint', 'full_access'], 
            scope, 
            {"project": project_id}
        )
        
        if not has_permission:
            raise PermissionDenied("You do not have permission to sync with SharePoint.")
        
        # Continue with the original logic
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project_id = serializer.validated_data.get('project_id')
        company_id = serializer.validated_data.get('company_id')
        freelancer_id = serializer.validated_data.get('freelancer_id')
        site_id = serializer.validated_data['site_id']
        site_name = serializer.validated_data['site_name']
        user = request.user
        if not project_id and not company_id and not freelancer_id:
            return Response({"error": "Project id or company_id or freelancer id is required"}, status=400)
        if project_id and (company_id or freelancer_id):
            return Response({"error": "Either Project id or (company_id or freelancer_id) is required"}, status=400)
        if not project_id and company_id and freelancer_id:
            return Response({"error": "Either company_id or freelancer id is required"}, status=400)

        try:
            azure_token = AzureToken.objects.get(user=user)
        except AzureToken.DoesNotExist:
            return Response({"error": "User not authenticated with Azure"}, status=status.HTTP_401_UNAUTHORIZED)

        headers = {
            "Authorization": f"Bearer {azure_token.access_token}"
        }

        # Fetch the root items
        root_url = MS_GRAPH_DRIVE_CHILDREN_URL.format(site_id=site_id)
        response = requests.get(root_url, headers=headers)
        if response.status_code != 200:
            return Response({"error": "Failed to fetch SharePoint site data"}, status=status.HTTP_400_BAD_REQUEST)

        data = response.json().get('value', [])

        if project_id:
            # Create or get the project root folder
            try:
                project = Project.objects.get(id=project_id)
                product_name = get_product_name_from_request(self.request)

                project_permission = project.team_members.filter(team_member=user).exists()
                if not project_permission:
                    raise PermissionDenied("You do not have permission to sync SharePoint data for this project.")
                project.sharepoint_site_id = site_id
                project.sharepoint_site_name = site_name
                project.save()
            except Project.DoesNotExist:
                return Response({"error": "Project not found"}, status=status.HTTP_404_NOT_FOUND)

            root_folder = Folder.objects.get(name=f"{project_id}_root_folder")
            
            # Create the sharepoint_data folder
            sharepoint_main_folder, created = Folder.objects.get_or_create(
                project=project,
                name="sharepoint_data",
                parent=root_folder
            )

            sharepoint_site_folder, created = Folder.objects.get_or_create(
                project=project,
                name=site_name,
                parent=sharepoint_main_folder
            )

            fetch_sharepoint_data.delay(data, sharepoint_site_folder.id, site_id, headers, user.id, project_id=project.id)


        elif company_id:
            try:
                company = CompanyProfile.objects.get(id=company_id)
                if user.company != company:
                    raise PermissionDenied("You do not have permission to sync SharePoint data for this company.")
            except CompanyProfile.DoesNotExist:
                return Response({"error": "Company not found"}, status=status.HTTP_404_NOT_FOUND)

            root_folder = Folder.objects.get(name=f"{company_id}_company_root_folder")

            
            # Create the sharepoint_data folder
            sharepoint_main_folder, created = Folder.objects.get_or_create(
                company_profile=company,
                name="sharepoint_data",
                parent=root_folder
            )

            sharepoint_site_folder, created = Folder.objects.get_or_create(
                company_profile=company,
                name=site_name,
                parent=sharepoint_main_folder
            )
            fetch_sharepoint_data.delay(data, sharepoint_site_folder.id, site_id, headers, user.id, company_id=company.id)

        elif freelancer_id:
            try:
                freelancer = User.objects.get(id=freelancer_id)
                if not user.is_freelancer or user != freelancer:
                    raise PermissionDenied("You do not have permission to sync SharePoint data for this freelancer.")
            except User.DoesNotExist:
                return Response({"error": "Freelancer not found"}, status=status.HTTP_404_NOT_FOUND)

            root_folder = Folder.objects.get(name=f"{freelancer_id}_freelancer_root_folder")

            # Create the sharepoint_data folder
            sharepoint_main_folder, created = Folder.objects.get_or_create(
                freelancer=freelancer,
                name="sharepoint_data",
                parent=root_folder
            )
            sharepoint_site_folder, created = Folder.objects.get_or_create(
                freelancer=freelancer,
                name=site_name,
                parent=sharepoint_main_folder
            )

            fetch_sharepoint_data.delay(data, sharepoint_site_folder.id, site_id, headers, user.id, freelancer_id=freelancer.id)  

        return Response({"message": "SharePoint data is being fetch in background."}, status=status.HTTP_200_OK)


class AzureTokenRefreshAPIView(APIView):
    def post(self, request):
        user = request.user  # Get the current user
        azure_token = AzureToken.objects.get(user=user)

        if not azure_token.is_expired():
            return Response({"message": "Access token is still valid."})

        # If expired, refresh the access token
        token_url = MS_OAUTH_TENANT_TOKEN_URL.format(tenant_id=settings.AZURE_TENANT_ID)
        token_data = {
            'grant_type': 'refresh_token',
            'refresh_token': azure_token.refresh_token,
            'client_id': settings.AZURE_CLIENT_ID,
            'client_secret': settings.AZURE_CLIENT_SECRET,
            'scope': 'offline_access Files.Read Sites.Read.All'
        }

        token_response = requests.post(token_url, data=token_data)

        if token_response.status_code == 200:
            token_info = token_response.json()
            azure_token.update_token_info(token_info)
            return Response({"message": "Access token refreshed successfully"})
        else:
            return Response({"error": "Failed to refresh access token"}, status=status.HTTP_400_BAD_REQUEST)
