from django.apps import AppConfig


class CompanyProfileApisConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'companyprofileapis'

    def ready(self):
        import companyprofileapis.signals 