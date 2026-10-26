from django.db import migrations, models


def _next_label(labels):
    """A, B, ... Z, AA, AB ... after the highest existing label (temporary default sequence)."""
    if not labels:
        return "A"
    letters = list(max(labels, key=lambda label: (len(label), label)))
    index = len(letters) - 1
    while index >= 0:
        if letters[index] == "Z":
            letters[index] = "A"
            index -= 1
        else:
            letters[index] = chr(ord(letters[index]) + 1)
            return "".join(letters)
    return "A" + "".join(letters)


def populate_versions_and_revisions(apps, schema_editor):
    Revision = apps.get_model("project_management", "DocumentRevision")
    Workflow = apps.get_model("project_management", "DocumentWorkflow")

    # VERSION numbers: 1, 2, 3... per document in creation order (created_at, id).
    document_ids = (
        Revision.objects.order_by().values_list("document_id", flat=True).distinct()
    )
    for document_id in document_ids:
        ordered = Revision.objects.filter(document_id=document_id).order_by(
            "created_at", "id"
        )
        for number, revision in enumerate(ordered, start=1):
            Revision.objects.filter(pk=revision.pk).update(version_number=number)

    # REVISION labels: only for versions with a completed APPROVED internal workflow,
    # in approval order (completed_at, workflow id). Nothing is invented on ambiguity.
    approved = Workflow.objects.filter(status="APPROVED").order_by(
        "revision__document_id", "completed_at", "id"
    )
    seen_revisions = set()
    by_document = {}
    for workflow in approved:
        if workflow.completed_at is None:
            raise RuntimeError(
                f"Ambiguous legacy data: approved workflow {workflow.pk} has no completed_at."
            )
        if workflow.revision_id in seen_revisions:
            raise RuntimeError(
                f"Ambiguous legacy data: revision {workflow.revision_id} has several approved workflows."
            )
        seen_revisions.add(workflow.revision_id)
        by_document.setdefault(workflow.revision.document_id, []).append(workflow)
    for workflows in by_document.values():
        labels = []
        for workflow in workflows:
            label = _next_label(labels)
            labels.append(label)
            Revision.objects.filter(pk=workflow.revision_id).update(
                revision_label=label
            )


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0194_documentworkflow"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentrevision",
            name="version_number",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="documentrevision",
            name="revision_label",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.RunPython(populate_versions_and_revisions, migrations.RunPython.noop),
    ]
