from django.contrib import admin
from models.models import Notification, NotificationSeen
from models.models import Preference

admin.site.register(Notification)
admin.site.register(Preference)
admin.site.register(NotificationSeen)