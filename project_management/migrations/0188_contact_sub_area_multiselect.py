from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


CONTACT_MODELS = ("ContactUser", "ContactCompany")


def copy_sub_area_values_to_array(apps, schema_editor):
    for model_name in CONTACT_MODELS:
        model = apps.get_model("project_management", model_name)
        for contact in model.objects.exclude(
            sub_area_of_expertise__isnull=True
        ).exclude(sub_area_of_expertise="").iterator():
            contact.sub_area_of_expertise_values = [
                contact.sub_area_of_expertise
            ]
            contact.save(update_fields=["sub_area_of_expertise_values"])


def restore_single_sub_area_value(apps, schema_editor):
    models_to_restore = [
        apps.get_model("project_management", model_name)
        for model_name in CONTACT_MODELS
    ]
    if any(
        model.objects.filter(
            sub_area_of_expertise_values__len__gt=1,
        ).exists()
        for model in models_to_restore
    ):
        raise IrreversibleError(
            "Cannot reverse project_management.0188 while contacts have "
            "multiple sub-areas of expertise. Remove the additional "
            "selections explicitly before retrying the rollback."
        )

    for model in models_to_restore:
        for contact in model.objects.exclude(
            sub_area_of_expertise_values=[]
        ).iterator():
            contact.sub_area_of_expertise = (
                contact.sub_area_of_expertise_values[0]
            )
            contact.save(update_fields=["sub_area_of_expertise"])


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0187_merge_contact_expertise_and_company_root"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactuser",
            name="sub_area_of_expertise_values",
            field=ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="contactcompany",
            name="sub_area_of_expertise_values",
            field=ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.RunPython(
            copy_sub_area_values_to_array,
            reverse_code=restore_single_sub_area_value,
        ),
        migrations.RemoveField(
            model_name="contactuser",
            name="sub_area_of_expertise",
        ),
        migrations.RemoveField(
            model_name="contactcompany",
            name="sub_area_of_expertise",
        ),
        migrations.RenameField(
            model_name="contactuser",
            old_name="sub_area_of_expertise_values",
            new_name="sub_area_of_expertise",
        ),
        migrations.RenameField(
            model_name="contactcompany",
            old_name="sub_area_of_expertise_values",
            new_name="sub_area_of_expertise",
        ),
    ]
