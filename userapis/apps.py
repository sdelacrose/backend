from django.apps import AppConfig


class UserapisConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'userapis'

    def ready(self):
        import userapis.signals 