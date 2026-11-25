"""PROSEVIEW MOD-01 scope correction: action templates move from the project to the COMPANY.

Compatible with 0236 already applied locally (0236 is NOT rewritten). Templates are kept: the
company is read from the project that owned each template; the project becomes provenance only
(`origin_project`). Titles that collide inside one company (templates from several projects) are
NOT merged: every member of a colliding group is flagged `needs_review` and reported.
"""
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def attach_company_and_flag_collisions(apps, schema_editor):
    ActionTemplate = apps.get_model("project_management", "ActionTemplate")
    groups = {}
    for template in ActionTemplate.objects.select_related("origin_project").order_by("id"):
        project = template.origin_project
        company_id = project.company_profile_id if project is not None else None
        if company_id is not None:
            ActionTemplate.objects.filter(pk=template.pk).update(company_id=company_id)
            groups.setdefault((company_id, template.normalized_title), []).append(template)
        else:
            # A project without a company cannot own a company template: keep it, flagged.
            ActionTemplate.objects.filter(pk=template.pk).update(needs_review=True)
            print(f"  [action templates] template {template.pk} has no company: flagged for review")
    for (company_id, title), members in groups.items():
        if len(members) > 1:
            ids = [m.pk for m in members]
            ActionTemplate.objects.filter(pk__in=ids).update(needs_review=True)
            print(
                f"  [action templates] company {company_id}: title collision {title!r} "
                f"between templates {ids} (from projects "
                f"{[m.origin_project_id for m in members]}): all flagged for review, nothing merged"
            )
    if schema_editor.connection.vendor == "postgresql":
        # The updates above queue deferred FK checks; PostgreSQL refuses the CREATE INDEX of the
        # next operation while they are pending, so run them now.
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0141_leavetype_is_sst_managed"),
        ("project_management", "0236_action_templates_progress"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="actiontemplate", name="unique_action_template_title_per_project"
        ),
        migrations.AddField(
            model_name="actiontemplate",
            name="company",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="action_templates", to="models.companyprofile",
            ),
        ),
        migrations.AddField(
            model_name="actiontemplate",
            name="needs_review",
            field=models.BooleanField(default=False),
        ),
        migrations.RenameField(model_name="actiontemplate", old_name="project", new_name="origin_project"),
        migrations.AlterField(
            model_name="actiontemplate",
            name="origin_project",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="origin_action_templates", to="project_management.project",
            ),
        ),
        migrations.RunPython(attach_company_and_flag_collisions, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="actiontemplate",
            constraint=models.UniqueConstraint(
                condition=Q(("needs_review", False)),
                fields=("company", "normalized_title"),
                name="unique_action_template_title_per_company",
            ),
        ),
    ]
