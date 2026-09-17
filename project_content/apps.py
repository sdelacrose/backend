from django.apps import AppConfig


class ProjectContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "project_content"

    def ready(self):
        from . import signals  # noqa: F401
