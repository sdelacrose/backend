import uuid
from django.db import models
from django.utils.timezone import now
from datetime import timedelta

from models.models import User

class AzureToken(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="azure_tokens")
    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField()

    def is_expired(self):
        return self.expires_at < now()

    def update_token_info(self, token_info):
        self.access_token = token_info.get("access_token")
        self.refresh_token = token_info.get("refresh_token")
        self.expires_at = now() + timedelta(seconds=token_info.get("expires_in"))
        self.save()

class OAuthState(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='oauth_states')
    state = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.state}"
    
    class Meta:
        ordering = ['created_at']