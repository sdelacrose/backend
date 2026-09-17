from django.apps import AppConfig


class HumanResourceManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'human_resource_management'

    def ready(self):
        import human_resource_management.signals