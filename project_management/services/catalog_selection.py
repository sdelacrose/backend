from django.db import transaction
from project_management.models import (
    CompanySolutionSpaceItem,
    Project,
    ProjectSolutionSpaceItem,
)


@transaction.atomic
def select_company_assembly(source, project, actor, *, refresh_existing=True):
    """Select the approved subtree using the same rules as manual catalog selection."""
    project = Project.objects.select_for_update().get(pk=project.pk)
    if source.company_profile_id != project.company_profile_id:
        raise ValueError("Catalog and project must belong to the same company")
    selected_by_source_id = {
        item.source_company_item_id: item
        for item in ProjectSolutionSpaceItem.objects.select_for_update().filter(
            project=project,
            source_company_item__isnull=False,
        )
    }
    source_rows = []
    pending_parent_ids = [source.id]
    while pending_parent_ids:
        batch = list(
            CompanySolutionSpaceItem.objects.filter(
                company_profile=source.company_profile,
                id__in=pending_parent_ids,
            ).order_by("level", "sort_order", "id")
        )
        source_rows.extend(batch)
        pending_parent_ids = list(
            CompanySolutionSpaceItem.objects.filter(
                parent_id__in=[item.id for item in batch],
                assembly_type=source.assembly_type,
            ).values_list("id", flat=True)
        )

    root_created = False
    copied = None
    for source_row in sorted(
        source_rows,
        key=lambda item: (item.level, item.sort_order, item.id),
    ):
        selected = selected_by_source_id.get(source_row.id)
        if selected and not refresh_existing:
            # Adding its parent must restore the selected subtree hierarchy, but
            # keep the existing row, linked requirements and project metadata.
            parent = selected_by_source_id.get(source_row.parent_id)
            if parent:
                selected.parent = parent
                selected.level = parent.level + 1
                selected.save(update_fields=["parent", "level", "updated_at"])
            continue
        was_existing = selected is not None
        parent = selected_by_source_id.get(source_row.parent_id)
        responsible_engineer = (
            source_row.responsible_engineer
            if source_row.responsible_engineer_id
            and (
                project.user_id == source_row.responsible_engineer_id
                or project.team_members.filter(
                    team_member_id=source_row.responsible_engineer_id,
                ).exists()
            )
            else None
        )
        defaults = {
            "parent": parent,
            "assembly_name": source_row.assembly_name,
            "required_skills": list(source_row.required_skills or []),
            "provided_skills": list(source_row.provided_skills or []),
            "responsible_engineer": responsible_engineer,
            "year_of_construction": source_row.year_of_construction,
            "assembly_type": source_row.assembly_type,
            "custom_fields": dict(source_row.custom_fields or {}),
            "level": parent.level + 1 if parent else 1,
            "sort_order": source_row.sort_order,
            "modified_by": actor,
        }
        if selected:
            for field, value in defaults.items():
                setattr(selected, field, value)
            selected.save(update_fields=[*defaults.keys(), "updated_at"])
        else:
            selected = ProjectSolutionSpaceItem.objects.create(
                project=project,
                source_company_item=source_row,
                created_by=actor,
                **defaults,
            )
            selected_by_source_id[source_row.id] = selected
        selected.customers.set(source_row.customers.all())
        source_row.projects.add(project)
        if source_row.id == source.id:
            copied = selected
            root_created = not was_existing

    copied = copied or selected_by_source_id[source.id]
    return copied, root_created
