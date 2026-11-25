from django.apps import AppConfig


class ProjectManagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'project_management'

    def ready(self):
        import project_management.signals
        import project_management.action_progress_signals  # PROSEVIEW MOD-01
