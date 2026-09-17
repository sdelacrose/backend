from django.db import models
from model_utils import FieldTracker
from models.models import Staff, CompanyProfile, User

class StaffObservation(models.Model):
    company_profile = models.ForeignKey(CompanyProfile, related_name="staff_observations", on_delete=models.CASCADE)
    title = models.CharField(max_length=500)
    description = models.CharField(max_length=1000, null=True, blank=True)
    staff = models.ForeignKey(Staff, related_name="staff_observations", on_delete=models.CASCADE)

    created_by = models.ForeignKey(User, related_name="created_staff_observations", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()