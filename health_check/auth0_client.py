import requests
import json
import os
from django.conf import settings
from typing import Optional


def get_auth0_token() -> Optional[str]:
    """
    Get a fresh Auth0 token using password realm grant type.
    Uses environment variables for credentials.
    """
    try:
        # Auth0 configuration from settings
        auth0_domain = settings.AUTH0_DOMAIN
        client_id = settings.AUTH0_M2M_CLIENT_ID
        client_secret = settings.AUTH0_M2M_CLIENT_SECRET
        audience = settings.AUTH0_AUDIENCE
        realm = settings.AUTH0_CONNECTION
        username = settings.HEALTH_CHECK_AUTH0_USERNAME
        password = settings.HEALTH_CHECK_AUTH0_PASSWORD

        
        if not username or not password:
            raise ValueError("HEALTH_CHECK_AUTH0_USERNAME and HEALTH_CHECK_AUTH0_PASSWORD must be set in environment")
        
        url = f"https://{auth0_domain}/oauth/token"
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        data = {
            'grant_type': 'http://auth0.com/oauth/grant-type/password-realm',
            'username': username,
            'password': password,
            'audience': audience,
            'client_id': client_id,
            'client_secret': client_secret,
            'realm': realm
        }
        
        response = requests.post(url, headers=headers, data=data, timeout=30)
        
        if response.status_code == 200:
            token_data = response.json()
            return token_data.get('access_token')
        else:
            print(f"Auth0 token request failed: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        print(f"Error getting Auth0 token: {str(e)}")
        return None


def get_headers_with_fresh_token() -> dict:
    """
    Get headers with a fresh Auth0 token.
    Falls back to existing token if Auth0 request fails.
    """
    token = get_auth0_token()
    
    if token:
        return {
            "Authorization": f"Bearer {token}",
            "X-API": settings.HEALTH_CHECK_API_KEY,
        }


# Token caching for performance
_cached_token = None
_token_expiry = None


def get_cached_auth0_token() -> Optional[str]:
    """
    Get Auth0 token with caching to avoid excessive API calls.
    Cache token for 5 minutes.
    """
    import time
    global _cached_token, _token_expiry
    
    current_time = time.time()
    
    # Return cached token if still valid (5 minutes cache)
    if _cached_token and _token_expiry and current_time < _token_expiry:
        return _cached_token
    
    # Get fresh token
    token = get_auth0_token()
    if token:
        _cached_token = token
        _token_expiry = current_time + 3000  # 5 minutes
        return token
    
    return None


def get_headers_with_cached_token() -> dict:
    """
    Get headers with cached Auth0 token.
    """
    token = get_cached_auth0_token()
    
    if token:
        return {
            "Authorization": f"Bearer {token}",
            "X-API": settings.HEALTH_CHECK_API_KEY,
        } 