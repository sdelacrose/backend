"""LINKS-RECETTE-05AL: the licence tables read by ecpmpapp.permissions.LicensePermission.

In every other environment these tables belong to the back-office (admin-server-backend), which runs
the `package_license` migrations. Recette has no back-office, so the models and the 22 migrations
are the unchanged files of admin-server-backend @ c27b6ab (package_license/__init__.py, models/,
migrations/), with the same app label so the schema stays compatible with it. Their signals, views,
webhooks and Stripe/admin_center code are deliberately not copied. Enabled only by
ecpmpapp.settings.recette.
"""
from django.apps import AppConfig


class RecettePackageLicenseConfig(AppConfig):
    name = "package_license"
    default_auto_field = "django.db.models.BigAutoField"
