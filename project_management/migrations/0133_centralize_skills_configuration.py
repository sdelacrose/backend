import django.contrib.postgres.fields
from django.db import migrations, models

import project_management.models.project


def merge_unique(*groups):
    merged = []
    seen = set()
    for group in groups:
        for value in group or []:
            normalized = str(value).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
    return merged


def migrate_skill_catalogs(apps, schema_editor):
    CompanyConfig = apps.get_model("models", "CompanyConfig")
    ECPMPConfig = apps.get_model("project_management", "ECPMPConfig")
    ProjectMatrixConfig = apps.get_model(
        "project_management",
        "ProjectMatrixConfig",
    )
    SolutionSpaceConfig = apps.get_model(
        "project_management",
        "SolutionSpaceConfig",
    )

    project_ids = set(
        ProjectMatrixConfig.objects.values_list("project_id", flat=True)
    )
    project_ids.update(
        SolutionSpaceConfig.objects.filter(project__isnull=False).values_list(
            "project_id",
            flat=True,
        )
    )

    for project_id in project_ids:
        project_config, _ = ECPMPConfig.objects.get_or_create(project_id=project_id)
        matrix_skills = ProjectMatrixConfig.objects.filter(
            project_id=project_id,
        ).values_list("requirement_skill_options", flat=True).first()
        solution_space_skills = SolutionSpaceConfig.objects.filter(
            project_id=project_id,
        ).values_list("skill_options", flat=True).first()
        project_config.skills = merge_unique(
            project_config.skills,
            matrix_skills,
            solution_space_skills,
        )
        project_config.save(update_fields=["skills"])

    company_skill_catalogs = SolutionSpaceConfig.objects.filter(
        project__isnull=True,
        company_profile__isnull=False,
    ).values_list("company_profile_id", "skill_options")
    for company_profile_id, skill_options in company_skill_catalogs:
        for company_config in CompanyConfig.objects.filter(
            company_profile_id=company_profile_id,
        ):
            company_config.skills = merge_unique(
                company_config.skills,
                skill_options,
            )
            company_config.save(update_fields=["skills"])


def restore_module_skill_catalogs(apps, schema_editor):
    CompanyConfig = apps.get_model("models", "CompanyConfig")
    ECPMPConfig = apps.get_model("project_management", "ECPMPConfig")
    ProjectMatrixConfig = apps.get_model(
        "project_management",
        "ProjectMatrixConfig",
    )
    SolutionSpaceConfig = apps.get_model(
        "project_management",
        "SolutionSpaceConfig",
    )

    for matrix_config in ProjectMatrixConfig.objects.all():
        project_config = ECPMPConfig.objects.filter(
            project_id=matrix_config.project_id,
        ).first()
        if project_config:
            matrix_config.requirement_skill_options = project_config.skills
            matrix_config.save(update_fields=["requirement_skill_options"])

    for solution_space_config in SolutionSpaceConfig.objects.all():
        if solution_space_config.project_id:
            project_config = ECPMPConfig.objects.filter(
                project_id=solution_space_config.project_id,
            ).first()
            skills = project_config.skills if project_config else []
        else:
            company_config = CompanyConfig.objects.filter(
                company_profile_id=solution_space_config.company_profile_id,
            ).first()
            skills = company_config.skills if company_config else []
        solution_space_config.skill_options = skills
        solution_space_config.save(update_fields=["skill_options"])


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0080_globalconfig_companyconfig_skills"),
        ("project_management", "0132_update_solution_space_french_ensemble_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="ecpmpconfig",
            name="skills",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=project_management.models.project.default_requirement_skill_options,
                size=None,
            ),
        ),
        migrations.RunPython(
            migrate_skill_catalogs,
            restore_module_skill_catalogs,
        ),
        migrations.RemoveField(
            model_name="projectmatrixconfig",
            name="requirement_skill_options",
        ),
        migrations.RemoveField(
            model_name="solutionspaceconfig",
            name="skill_options",
        ),
    ]
