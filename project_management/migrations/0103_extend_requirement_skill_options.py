# Generated manually to append new default matrix requirement skills.

from django.db import migrations


DEFAULT_REQUIREMENT_SKILLS = [
    "blow",
    "push",
    "move",
    "turn",
    "lift",
    "lay down",
    "heat up",
    "transport",
    "deburr",
    "drill",
    "tighten",
    "Soldering",
]


def append_requirement_skills(apps, schema_editor):
    ProjectMatrixConfig = apps.get_model("project_management", "ProjectMatrixConfig")
    for config in ProjectMatrixConfig.objects.all():
        skills = list(config.requirement_skill_options or [])
        changed = False
        for skill in DEFAULT_REQUIREMENT_SKILLS:
            if skill not in skills:
                skills.append(skill)
                changed = True
        if changed:
            config.requirement_skill_options = skills
            config.save(update_fields=["requirement_skill_options"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0102_project_matrix"),
    ]

    operations = [
        migrations.RunPython(append_requirement_skills, migrations.RunPython.noop),
    ]
