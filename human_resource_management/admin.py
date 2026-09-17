from django.contrib import admin
from human_resource_management.models import LeaveRequest
from models.models import Staff

# Register your models here.
admin.site.register(LeaveRequest)
admin.site.register(Staff)
