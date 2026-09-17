from rest_framework import serializers
from models.models import Notification, NotificationSeen
from django.contrib.contenttypes.models import ContentType
from models.models import User, Preference

class NotificationSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=User.objects.all(),
        required=False
    )
    content_type = serializers.SlugRelatedField(
        slug_field='model',
        queryset=ContentType.objects.all()
    )
    object_id = serializers.IntegerField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            'id',
            'user',
            'message',
            'created_at',
            'updated_at',
            'important',
            'type',
            'content_type',
            'object_id',
            'status',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'message', 'type']
    
    def get_status(self, obj):
        return getattr(obj, "seen_status", NotificationSeen.STATUS_CHOICES[0][0])
        
    def to_representation(self, instance):
        repersentation = super().to_representation(instance)
        return repersentation
        
class NotificationSeenSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all()
    )
    notification = serializers.PrimaryKeyRelatedField(
        queryset=Notification.objects.all()
    )

    class Meta:
        model = NotificationSeen
        fields = [
            'id',
            'user',
            'notification',
            'status',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

class PreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Preference
        fields = [
            'id',
            'user',
            'notify_item_created_by_me',
            'notify_item_i_own',
            'notify_item_am_part',
            'specific_notify_item_i_own',
            'specific_notify_item_am_part_of',
            'specific_notify_company_item_i_own',
            'specific_notify_company_item_am_part_of',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['user'] 
