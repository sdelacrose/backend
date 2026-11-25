"""LOT 1 (client surveillance A/B/C/D): additive model for the explicit filiation.

- DocumentRevision.index_revision: a technical version (the BPE file of an accepted index) shares the index of an
  approved revision without taking its label (revision_label stays unique); three check constraints keep it coherent.
- ClientSurveillanceFollowUp.document / .client_company: derived from the return's ClientIssue, immutable; they carry
  the targeted guarantee "one open follow-up per (document, client company)" without serialising other clients.
- ClientSurveillanceFollowUp.expected_index: the documentary index expected from the derived revision (null = legacy).

Existing follow-ups are backfilled ONLY from their own return (return -> ClientIssue -> revision -> document, and the
issue's client company), after checking that the surveillance, the issue and the derived revision agree. Any
disagreement stops the migration with an explicit message: no arbitrary value is ever written.
Reversible: the backward operations drop the constraints and the columns; no data of the 0229-0256 chain is touched.
"""
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F, Q


def backfill_followups(apps, schema_editor):
    FollowUp = apps.get_model("project_management", "ClientSurveillanceFollowUp")
    problems = []
    for followup in FollowUp.objects.select_related(
        "client_return__client_issue__document_revision", "surveillance", "derived_revision"
    ).order_by("pk"):
        issue = followup.client_return.client_issue
        document_id = issue.document_revision.document_id
        if followup.surveillance.client_issue_id != issue.pk:
            problems.append(f"follow-up {followup.pk}: its surveillance and its return belong to different client issues")
            continue
        if followup.derived_revision_id and followup.derived_revision.document_id != document_id:
            problems.append(f"follow-up {followup.pk}: its derived revision belongs to another document")
            continue
        followup.document_id = document_id
        followup.client_company_id = issue.client_company_id
        followup.save(update_fields=["document", "client_company"])
    if problems:
        raise RuntimeError("Ambiguous follow-up attachment, nothing was migrated: " + "; ".join(problems))


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0256_client_surveillance_followup"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentrevision",
            name="index_revision",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="technical_versions", to="project_management.documentrevision",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentrevision",
            constraint=models.CheckConstraint(
                condition=Q(index_revision__isnull=True) | ~Q(index_revision=F("id")),
                name="document_revision_index_not_self",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentrevision",
            constraint=models.CheckConstraint(
                condition=Q(index_revision__isnull=True) | Q(revision_label__isnull=True),
                name="document_revision_index_has_no_own_label",
            ),
        ),
        migrations.AddConstraint(
            model_name="documentrevision",
            constraint=models.CheckConstraint(
                condition=Q(index_revision__isnull=True) | Q(issue_state="BPE"),
                name="document_revision_index_is_bpe",
            ),
        ),
        migrations.AddField(
            model_name="clientsurveillancefollowup",
            name="expected_index",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="clientsurveillancefollowup",
            name="document",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="client_followups", to="project_management.document",
            ),
        ),
        migrations.AddField(
            model_name="clientsurveillancefollowup",
            name="client_company",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="client_followups", to="project_management.contactcompany",
            ),
        ),
        migrations.RunPython(backfill_followups, migrations.RunPython.noop),
        # PostgreSQL refuses ALTER TABLE while deferred foreign-key checks of the rows just updated are pending.
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="clientsurveillancefollowup",
            name="document",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="client_followups", to="project_management.document",
            ),
        ),
        migrations.AlterField(
            model_name="clientsurveillancefollowup",
            name="client_company",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="client_followups", to="project_management.contactcompany",
            ),
        ),
        migrations.AddConstraint(
            model_name="clientsurveillancefollowup",
            constraint=models.UniqueConstraint(
                condition=Q(("closed_at__isnull", True)), fields=("document", "client_company"),
                name="client_followup_one_open_per_document_client",
            ),
        ),
    ]
