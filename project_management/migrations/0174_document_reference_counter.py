from collections import defaultdict

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def _resolve_folder_owner(folder_id, folders, resolved, visiting):
    if folder_id in resolved:
        return resolved[folder_id]
    if folder_id in visiting:
        raise RuntimeError("Folder hierarchy cycle detected while backfilling document numbering.")

    folder = folders[folder_id]
    owners = [
        ("project", folder.project_id) if folder.project_id else None,
        ("company_profile", folder.company_profile_id)
        if folder.company_profile_id
        else None,
        ("freelancer", folder.freelancer_id) if folder.freelancer_id else None,
    ]
    owners = [owner for owner in owners if owner is not None]
    if len(owners) > 1:
        raise RuntimeError(f"Folder {folder_id} belongs to multiple document libraries.")
    direct_owner = owners[0] if owners else None
    parent_owner = None
    if folder.parent_id:
        visiting.add(folder_id)
        parent_owner = _resolve_folder_owner(
            folder.parent_id,
            folders,
            resolved,
            visiting,
        )
        visiting.remove(folder_id)

    if direct_owner and parent_owner and direct_owner != parent_owner:
        raise RuntimeError(
            f"Folder {folder_id} crosses document library boundaries."
        )

    owner = direct_owner or parent_owner
    resolved[folder_id] = owner
    return owner


def _replace_sequence(full_document_number, sequence_number, revision):
    value = str(full_document_number or "").strip()
    if not value:
        return value

    parts = value.rsplit("-", 2)
    revision_number = int(revision or 0)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return f"{parts[0]}-{sequence_number:05d}-{revision_number:02d}"
    suffix = f"-{sequence_number:05d}-{revision_number:02d}"
    return f"{value[: 255 - len(suffix)]}{suffix}"


def backfill_document_reference_counters(apps, schema_editor):
    Folder = apps.get_model("project_management", "Folder")
    Document = apps.get_model("project_management", "Document")
    DocumentReferenceCounter = apps.get_model(
        "project_management",
        "DocumentReferenceCounter",
    )
    database_alias = schema_editor.connection.alias

    folders = {
        folder.id: folder
        for folder in Folder.objects.using(database_alias).all().only(
            "id",
            "parent_id",
            "project_id",
            "company_profile_id",
            "freelancer_id",
        )
    }
    resolved_owners = {}
    folders_to_update = []
    for folder in folders.values():
        owner = _resolve_folder_owner(
            folder.id,
            folders,
            resolved_owners,
            set(),
        )
        if owner is None:
            continue
        if not any(
            (
                folder.project_id,
                folder.company_profile_id,
                folder.freelancer_id,
            )
        ):
            owner_type, owner_id = owner
            setattr(folder, f"{owner_type}_id", owner_id)
            folders_to_update.append(folder)

    if folders_to_update:
        Folder.objects.using(database_alias).bulk_update(
            folders_to_update,
            ["project", "company_profile", "freelancer"],
        )

    documents_by_scope = defaultdict(list)
    documents = (
        Document.objects.using(database_alias)
        .select_related("folder")
        .order_by("uploaded_at", "id")
    )
    for document in documents.iterator():
        folder = document.folder
        owners = [
            ("project", folder.project_id) if folder.project_id else None,
            ("company_profile", folder.company_profile_id)
            if folder.company_profile_id
            else None,
            ("freelancer", folder.freelancer_id) if folder.freelancer_id else None,
        ]
        owners = [owner for owner in owners if owner is not None]
        if len(owners) != 1:
            raise RuntimeError(
                f"Document {document.id} does not resolve to exactly one library."
            )
        documents_by_scope[owners[0]].append(document)

    now = timezone.now()
    for (owner_type, owner_id), scoped_documents in documents_by_scope.items():
        current_max = max(
            (
                int(document.sequence_number)
                for document in scoped_documents
                if document.sequence_number and int(document.sequence_number) > 0
            ),
            default=0,
        )
        used_sequences = set()
        used_references = set()
        documents_to_update = []

        for document in scoped_documents:
            sequence_number = int(document.sequence_number or 0)
            document_reference = str(document.full_document_number or "").strip()
            if sequence_number <= 0:
                continue

            needs_new_sequence = (
                sequence_number in used_sequences
                or (
                    document_reference
                    and document_reference in used_references
                )
            )

            if needs_new_sequence:
                current_max += 1
                sequence_number = current_max
                document.sequence_number = sequence_number
                document.full_document_number = _replace_sequence(
                    document_reference,
                    sequence_number,
                    document.revision,
                )
                document.updated_at = now
                documents_to_update.append(document)
                document_reference = str(
                    document.full_document_number or ""
                ).strip()

            used_sequences.add(sequence_number)
            if document_reference:
                used_references.add(document_reference)

        if documents_to_update:
            Document.objects.using(database_alias).bulk_update(
                documents_to_update,
                ["sequence_number", "full_document_number", "updated_at"],
            )

        counter_lookup = {
            "project_id": None,
            "company_profile_id": None,
            "freelancer_id": None,
        }
        counter_lookup[f"{owner_type}_id"] = owner_id
        DocumentReferenceCounter.objects.using(database_alias).update_or_create(
            defaults={"last_sequence": current_max},
            **counter_lookup,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0121_add_customer_demand_company_document_type"),
        ("project_management", "0173_update_assembly_type_french_label"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentReferenceCounter",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("last_sequence", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "company_profile",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="document_reference_counters",
                        to="models.companyprofile",
                    ),
                ),
                (
                    "freelancer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="document_reference_counters",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="document_reference_counters",
                        to="project_management.project",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="documentreferencecounter",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        project__isnull=False,
                        company_profile__isnull=True,
                        freelancer__isnull=True,
                    )
                    | models.Q(
                        project__isnull=True,
                        company_profile__isnull=False,
                        freelancer__isnull=True,
                    )
                    | models.Q(
                        project__isnull=True,
                        company_profile__isnull=True,
                        freelancer__isnull=False,
                    )
                ),
                name="document_reference_counter_single_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentreferencecounter",
            constraint=models.UniqueConstraint(
                fields=("project",),
                condition=models.Q(
                    project__isnull=False,
                    company_profile__isnull=True,
                    freelancer__isnull=True,
                ),
                name="unique_document_reference_counter_project",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentreferencecounter",
            constraint=models.UniqueConstraint(
                fields=("company_profile",),
                condition=models.Q(
                    project__isnull=True,
                    company_profile__isnull=False,
                    freelancer__isnull=True,
                ),
                name="unique_document_reference_counter_company",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentreferencecounter",
            constraint=models.UniqueConstraint(
                fields=("freelancer",),
                condition=models.Q(
                    project__isnull=True,
                    company_profile__isnull=True,
                    freelancer__isnull=False,
                ),
                name="unique_document_reference_counter_freelancer",
            ),
        ),
        migrations.RunPython(
            backfill_document_reference_counters,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
