from django.contrib import admin
from .models import CalendarEvent, UserDashboardConfig


# Register your models here.
admin.site.register(CalendarEvent)
admin.site.register(UserDashboardConfig)
