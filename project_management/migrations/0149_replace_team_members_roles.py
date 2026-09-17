from django.db import migrations

NEW_TEAM_MEMBERS_ROLES = [
    "consultant",
    "project manager",
    "customer",
    "supplier",
    "notified body",
]

LEGACY_DEFAULT_TEAM_MEMBERS_ROLES = {
    "architect",
    "structural engineer",
    "contractor",
    "project manager",
}

RENAME_TEAM_MEMBERS_ROLES = {
    "contractor": "consultant",
}


def _normalize_team_members_roles(current):
    values = [str(item).strip() for item in (current or []) if str(item).strip()]
    renamed = [RENAME_TEAM_MEMBERS_ROLES.get(item, item) for item in values]

    customs = []
    for item in renamed:
        if item in LEGACY_DEFAULT_TEAM_MEMBERS_ROLES:
            continue
        if item not in NEW_TEAM_MEMBERS_ROLES:
            customs.append(item)

    ordered = list(NEW_TEAM_MEMBERS_ROLES)
    seen = set(ordered)
    for custom in customs:
        if custom not in seen:
            ordered.append(custom)
            seen.add(custom)
    return ordered


def _collect_in_use_roles(team_members_model, external_users_model, project_id):
    used = set()
    for role in team_members_model.objects.filter(project_id=project_id).values_list(
        "role", flat=True
    ):
        if role:
            used.add(RENAME_TEAM_MEMBERS_ROLES.get(str(role).strip(), str(role).strip()))
    for role in external_users_model.objects.filter(project_id=project_id).values_list(
        "role", flat=True
    ):
        if role:
            used.add(RENAME_TEAM_MEMBERS_ROLES.get(str(role).strip(), str(role).strip()))
    return used


def replace_project_team_members_roles(apps, schema_editor):
    team_members_model = apps.get_model("project_management", "ProjectTeamMembers")
    external_users_model = apps.get_model("project_management", "ProjectExternalUsers")

    # Remap renamed role on existing assignments first.
    for model in (team_members_model, external_users_model):
        model.objects.filter(role="contractor").update(role="consultant")

    ecpmp_model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in ecpmp_model._meta.get_fields()}
    if "team_members_roles" not in field_names:
        return

    to_update = []
    for obj in ecpmp_model.objects.all().only("id", "project_id", "team_members_roles"):
        current = list(getattr(obj, "team_members_roles", []) or [])
        updated = _normalize_team_members_roles(current)

        # Keep obsolete roles still assigned on this project selectable (compatibility).
        seen = set(updated)
        for role in _collect_in_use_roles(
            team_members_model, external_users_model, obj.project_id
        ):
            if role and role not in seen:
                updated.append(role)
                seen.add(role)

        if updated != current:
            obj.team_members_roles = updated
            to_update.append(obj)

    if to_update:
        ecpmp_model.objects.bulk_update(to_update, ["team_members_roles"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0148_replace_expense_types"),
    ]

    operations = [
        migrations.RunPython(
            replace_project_team_members_roles,
            migrations.RunPython.noop,
        ),
    ]
