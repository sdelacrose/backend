from django.db import models
from django.core.exceptions import ValidationError

class AnnotationQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Annotations cannot be bulk-edited.')
    def delete(self):
        raise ValidationError('Annotations cannot be deleted.')

class SupplierReviewAnnotation(models.Model):
    objects = AnnotationQuerySet.as_manager()
    assignment = models.ForeignKey('SupplierSurveillanceReviewerAssignment', on_delete=models.PROTECT, related_name='annotations')
    visibility = models.CharField(max_length=12, choices=[("INTERNAL","Internal"),("EXTERNAL","Supplier")], default="INTERNAL")
    mentioned_reviewer = models.ForeignKey("models.User",null=True,blank=True,on_delete=models.PROTECT,related_name="supplier_review_mentions")
    item_snapshot_id = models.PositiveIntegerField()
    page = models.PositiveIntegerField()
    x = models.FloatField()
    y = models.FloatField()
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    retracted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['page', 'y', 'x', 'created_at', 'id']

    def save(self, *args, **kwargs):
        if self.pk:
            before = type(self).objects.get(pk=self.pk)
            for field in ('assignment_id','visibility','mentioned_reviewer_id','item_snapshot_id','page','x','y','text','created_at'):
                if getattr(before,field) != getattr(self,field):
                    raise ValidationError('Annotations are immutable; retract and add a new comment.')
            if before.retracted_at:
                raise ValidationError('This annotation has already been retracted.')
        if self.assignment.status == 'COMPLETED' or self.assignment.surveillance.status in ('READY_FOR_CLOSURE','CLOSED'):
            raise ValidationError('Completed review annotations are read-only.')
        super().save(*args, **kwargs)

    def delete(self,*args,**kwargs):
        raise ValidationError('Annotations must be retracted, never deleted.')
