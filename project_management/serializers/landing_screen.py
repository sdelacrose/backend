from rest_framework import serializers

from project_management.models import (
    Project
)


class LandingScreenSerializer(serializers.ModelSerializer):

    class Meta:
        model = Project
        fields = '__all__'
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        open_milestones = instance.milestones.exclude(status="closed").order_by('due_date')
        if open_milestones.exists():
            representation['next_milestone'] = {
                "name": open_milestones[0].name,
                "colour": open_milestones[0].colour,
                "due_date": open_milestones[0].due_date
            }
        else:
            representation['next_milestone'] = None
            
        open_action_items = 0
        for milestone in open_milestones:
            open_action_items += milestone.action_items.exclude(status="closed").count()
        representation["open_action_items"] = open_action_items
        representation["invoice_statuses"] = list(instance.project_bills.values_list("invoice_no", "invoice_status"))
        return representation

