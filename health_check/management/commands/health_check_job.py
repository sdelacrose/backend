# backend/management/commands/health_check_job.py
import requests
import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings
from health_check.auth0_client import get_headers_with_cached_token

# Import your utils if needed
try:
    from health_check.utils import override_base_url
except ImportError:
    # Fallback if utils not available
    def override_base_url(url):
        return url

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Health check API job - runs every 59 minutes'
    
    def handle(self, *args, **options):
        try:
            # Get the API key from the environment variable
            base_url = settings.BACKEND_URL 
            
            # Get headers with cached token
            headers = get_headers_with_cached_token()
            
            # Use the service name from docker-compose network
            url = override_base_url(f"{base_url.rstrip('/')}/api/health-check/status/test/")
            
            response = requests.get(url, headers=headers, timeout=600)
            
            timestamp = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
            message = f"[{timestamp}] [Health Check Cron] API called. Status: {response.status_code}"
            
            self.stdout.write(message)
            logger.info(message)
            
            if response.status_code == 200:
                self.stdout.write(
                    self.style.SUCCESS(f"[{timestamp}] Health check successful")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"[{timestamp}] Health check returned status {response.status_code}")
                )
                
        except requests.exceptions.RequestException as e:
            timestamp = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
            error_message = f"[{timestamp}] [Health Check Cron] Error calling API: {e}"
            
            self.stdout.write(error_message)
            logger.error(error_message)
            
            self.stdout.write(
                self.style.ERROR(f"[{timestamp}] Health check failed: {str(e)}")
            )
        except Exception as e:
            timestamp = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
            error_message = f"[{timestamp}] [Health Check Cron] Unexpected error: {e}"
            
            self.stdout.write(error_message)
            logger.error(error_message)
            
            self.stdout.write(
                self.style.ERROR(f"[{timestamp}] Health check failed with unexpected error: {str(e)}")
            ) 
