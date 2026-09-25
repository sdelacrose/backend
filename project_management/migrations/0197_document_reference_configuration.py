import re

from django.db import migrations, models

MAX_LENGTH = 10


def _derive(name):
    words = re.findall(r"[A-Za-z0-9]+", str(name or ""))
    if len(words) >= 2:
        return "".join(word[0] for word in words[:4]).upper()
    if words:
        return words[0][:3].upper()
    return "PRJ"


def propose_codes_for_existing_projects(apps, schema_editor):
    """Give every existing project a unique (per owner) code. No document reference is touched."""
    Project = apps.get_model("project_management", "Project")
    User = apps.get_model("models", "User")
    used = {}
    for project in Project.objects.order_by("id"):
        if project.project_code:
            continue
        company_id = project.company_profile_id
        if not company_id and project.user_id:
            company_id = (
                User.objects.filter(pk=project.user_id)
                .values_list("company_id", flat=True)
                .first()
            )
        owner = ("company", company_id) if company_id else ("user", project.user_id)
        if owner not in used:
            used[owner] = set(
                Project.objects.filter(
                    project_code__gt="",
                    **(
                        {"company_profile_id": company_id}
                        if company_id
                        else {"user_id": project.user_id, "company_profile__isnull": True}
                    ),
                ).values_list("project_code", flat=True)
            )
        base, candidate, number = _derive(project.project_name), None, 1
        candidate = base
        while candidate in used[owner]:
            number += 1
            suffix = str(number)
            candidate = f"{base[: MAX_LENGTH - len(suffix)]}{suffix}"
        used[owner].add(candidate)
        Project.objects.filter(pk=project.pk).update(project_code=candidate)


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0196_documentrevision_version_constraints"),
        ("models", "0127_offer_validity_periods"),
    ]

    operations = [
        migrations.AddField(
            model_name="ecpmpconfig",
            name="document_control_configuration",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RemoveConstraint(
            model_name="documentreferencecounter",
            name="unique_document_reference_counter_project",
        ),
        migrations.RemoveConstraint(
            model_name="documentreferencecounter",
            name="unique_document_reference_counter_company",
        ),
        migrations.RemoveConstraint(
            model_name="documentreferencecounter",
            name="unique_document_reference_counter_freelancer",
        ),
        migrations.AddField(
            model_name="documentreferencecounter",
            name="scope_key",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddConstraint(
            model_name="documentreferencecounter",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    project__isnull=False,
                    company_profile__isnull=True,
                    freelancer__isnull=True,
                ),
                fields=("project", "scope_key"),
                name="unique_document_reference_counter_project",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentreferencecounter",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    project__isnull=True,
                    company_profile__isnull=False,
                    freelancer__isnull=True,
                ),
                fields=("company_profile", "scope_key"),
                name="unique_document_reference_counter_company",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentreferencecounter",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    project__isnull=True,
                    company_profile__isnull=True,
                    freelancer__isnull=False,
                ),
                fields=("freelancer", "scope_key"),
                name="unique_document_reference_counter_freelancer",
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="project_code",
            field=models.CharField(blank=True, default="", max_length=10),
        ),
        migrations.RunPython(propose_codes_for_existing_projects, migrations.RunPython.noop),
    ]
