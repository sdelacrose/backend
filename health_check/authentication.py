from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.conf import settings


class APIKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        api_key = request.META.get('HTTP_X_API')
        if not api_key:
            raise AuthenticationFailed('API key not provided')
        
        if not self.is_valid_api_key(api_key):
            raise AuthenticationFailed('Invalid API key')
        
        # Return a tuple of (user, auth) - you can return None for user
        return (None, api_key)
    
    def is_valid_api_key(self, api_key):
        return api_key == getattr(settings, 'HEALTH_CHECK_API_KEY', None) 