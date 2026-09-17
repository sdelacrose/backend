from django.db import models
from model_utils import FieldTracker
from models.models import Staff

class LeaveRequest(models.Model):
    LEAVE_STATUSES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected')
    ]
    employee = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='leave_requests')
    manager = models.ForeignKey(
        Staff,
        on_delete=models.SET_NULL,
        related_name='approval_requests',
        null=True,
        blank=True,
    )
    type = models.CharField(max_length=50, null=True, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    is_working_from_home = models.BooleanField(default=False)
    status = models.CharField(choices=LEAVE_STATUSES, default='pending')
    reason = models.TextField(blank=True, null=True)
    tracker = FieldTracker()
    def __str__(self):
        return f"{self.employee.user.email} - {self.type} leave request"
