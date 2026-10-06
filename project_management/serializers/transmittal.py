from django.db import transaction
from rest_framework import serializers

from project_management.models import Transmittal, TransmittalItem
from project_management.services.transmittal_finalize import is_review_overdue
from project_management.services.transmittal_reference import generate_transmittal_number


class TransmittalItemSerializer(serializers.ModelSerializer):
    # LOT 3E-1B: `document_revision` alone is a bare PK -- fine for ISSUED evidence (frozen in
    # evidence_snapshot already), but a DRAFT item has no snapshot yet and the frontend would
    # otherwise have no document reference/title/revision label to show at all (a real API gap,
    # not a display choice -- see the LOT 3E-1B report). Read-only, additive: never touches
    # `document_revision` itself (still the plain writable PK add/remove already uses), and
    # mirrors build_item_evidence_snapshot's own field names/shape exactly, so the frontend can
    # treat a live revision and a frozen snapshot the same way once ISSUED.
    document_revision_detail = serializers.SerializerMethodField()
    # L3H-3: the frozen Client Review timing snapshot (review_duration_days/review_due_date are
    # plain model fields, null unless this was an OUT+FOR_REVIEW item at successful issuance)
    # plus the derived, never-persisted overdue condition (section 16/17/18/19/20) -- the
    # TransmittalItem serializer is the natural, exact-revision-correct source: never a
    # replacement for the existing Client business status derived elsewhere (L3F).
    is_overdue = serializers.SerializerMethodField()
    # HISTORY-1: the minimum useful read-only surface -- the stable Client Issue identity this
    # occurrence belongs to (survives retransmission) plus its label (NULL until a future lot
    # assigns one, never fabricated here). client_issue itself is a plain FK (writable only by
    # services.transmittal_finalize, never by this serializer -- no create/update endpoint for
    # it exists in this lot, section 21/22).
    client_revision_label = serializers.SerializerMethodField()

    class Meta:
        model = TransmittalItem
        fields = [
            "id", "transmittal", "document_revision", "document_revision_detail",
            "distribution_purpose", "evidence_snapshot", "created_at",
            "review_duration_days", "review_due_date", "is_overdue",
            "client_issue", "client_revision_label",
        ]
        read_only_fields = [
            "id", "transmittal", "document_revision_detail", "evidence_snapshot", "created_at",
            "review_duration_days", "review_due_date", "is_overdue",
            "client_issue", "client_revision_label",
        ]

    def get_is_overdue(self, obj):
        return is_review_overdue(obj.review_due_date)

    def get_client_revision_label(self, obj):
        return obj.client_issue.client_revision_label if obj.client_issue_id else None

    def get_document_revision_detail(self, obj):
        revision = obj.document_revision
        if revision is None:
            return None
        document = revision.document
        return {
            "document": {
                "id": document.pk,
                "reference": document.full_document_number,
                "file_name": document.file_name,
            },
            "revision": {
                "id": revision.pk,
                "version_number": revision.version_number,
                "revision_label": revision.revision_label,
                # D13: the external issuer's own revision designation (IN items, primarily).
                "external_revision": revision.external_revision,
                "file_name": revision.file_name,
            },
        }


class TransmittalSerializer(serializers.ModelSerializer):
    created_by = serializers.SerializerMethodField()
    items = TransmittalItemSerializer(many=True, read_only=True)
    # LOT 3E-1B: `project`/`external_company`/`external_contacts` are plain FK/M2M PKs -- fine
    # for create/update (the only place they need to be writable), but a read-only detail view
    # must never show a raw id (see the LOT 3E-1B report's "Do not expose raw IDs" requirement).
    # Additive only: the PK fields above are untouched and still what create/update read/write.
    project_name = serializers.SerializerMethodField()
    external_company_name = serializers.SerializerMethodField()
    external_contacts_detail = serializers.SerializerMethodField()

    class Meta:
        model = Transmittal
        fields = [
            "id", "transmittal_number", "direction", "status", "project", "project_name",
            "external_company", "external_company_name",
            "external_contacts", "external_contacts_detail", "subject", "message",
            "issued_at", "received_at", "evidence_snapshot", "items",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "project_name", "external_company_name", "external_contacts_detail",
            "issued_at", "received_at", "evidence_snapshot", "items",
            "created_by", "created_at", "updated_at",
        ]

    def get_created_by(self, obj):
        if not obj.created_by_id:
            return None
        return {"id": obj.created_by_id, "name": obj.created_by.full_name}

    def get_project_name(self, obj):
        return obj.project.project_name if obj.project_id else None

    def get_external_company_name(self, obj):
        return obj.external_company.company_name if obj.external_company_id else None

    def get_external_contacts_detail(self, obj):
        return [
            {"id": contact.pk, "name": f"{contact.name} {contact.surname}".strip()}
            for contact in obj.external_contacts.all()
        ]

    def _validate_contacts_belong_to_company(self, company, contacts):
        for contact in contacts or []:
            if contact.contact_company_id != (company.id if company else None):
                raise serializers.ValidationError(
                    {"external_contacts": "Every contact must belong to the selected external company."}
                )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance

        if instance is not None and instance.status == Transmittal.Status.ISSUED:
            raise serializers.ValidationError("An issued transmittal cannot be modified.")

        status = attrs.get("status", instance.status if instance else Transmittal.Status.DRAFT)
        if instance is None and status != Transmittal.Status.DRAFT:
            # The finalize action (issue/receive) is the ONLY path to ISSUED, for both
            # directions: it is what requires >=1 item, revalidates eligibility, and freezes
            # evidence.
            raise serializers.ValidationError(
                {"status": "A new transmittal is always created as a draft; use the issue/receive action to finalize it."}
            )

        direction = attrs.get("direction", instance.direction if instance else None)
        project = attrs.get("project", instance.project if instance else None)
        if project is None:
            raise serializers.ValidationError({"project": "A project is required."})

        external_company = attrs.get(
            "external_company", instance.external_company if instance else None
        )
        external_contacts = attrs.get(
            "external_contacts", list(instance.external_contacts.all()) if instance else []
        )
        self._validate_contacts_belong_to_company(external_company, external_contacts)

        # Tenant ownership: company_role is free text on ContactCompany and not relied on as
        # the integrity mechanism -- external_company must actually belong to the
        # Transmittal's own project (its company, or its freelancer library).
        owning_company, owning_freelancer = project.company_profile, project.user
        if external_company is not None:
            if owning_company is not None and external_company.company_profile_id != owning_company.id:
                raise serializers.ValidationError(
                    {"external_company": "This company does not belong to your organization."}
                )
            if (
                owning_company is None
                and owning_freelancer is not None
                and external_company.user_id != owning_freelancer.id
            ):
                raise serializers.ValidationError(
                    {"external_company": "This company does not belong to your library."}
                )

        if instance is None:
            number = attrs.get("transmittal_number") or ""
            if direction == Transmittal.Direction.OUT:
                if number:
                    raise serializers.ValidationError(
                        {"transmittal_number": "This is generated automatically for an outgoing transmittal."}
                    )
            else:
                # IN: the Supplier's own reference, entered manually -- required upfront (it
                # is part of the uniqueness key together with project + external_company).
                if not number.strip():
                    raise serializers.ValidationError(
                        {"transmittal_number": "The supplier's transmittal reference is required."}
                    )
                if external_company is None:
                    raise serializers.ValidationError(
                        {"external_company": "The supplier company is required."}
                    )
                if Transmittal.objects.filter(
                    project=project, external_company=external_company, transmittal_number=number,
                ).exists():
                    raise serializers.ValidationError(
                        {
                            "transmittal_number": (
                                "This supplier reference already exists for this project and "
                                "supplier."
                            )
                        }
                    )

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        user = request.user if request else self.context.get("user")
        external_contacts = validated_data.pop("external_contacts", [])
        validated_data["created_by"] = user
        with transaction.atomic():
            if validated_data["direction"] == Transmittal.Direction.OUT:
                validated_data["transmittal_number"] = generate_transmittal_number(
                    project=validated_data["project"],
                )
            # IN: validated_data["transmittal_number"] is already the supplier-supplied value
            # (validated in validate()); no counter is touched.
            instance = super().create(validated_data)
            if external_contacts:
                instance.external_contacts.set(external_contacts)
            from project_management.models import TransmittalEvent

            TransmittalEvent.objects.create(
                transmittal=instance, event_type=TransmittalEvent.EventType.CREATED, actor=user,
            )
        return instance

    def update(self, instance, validated_data):
        validated_data.pop("transmittal_number", None)  # permanent from creation (both directions)
        external_contacts = validated_data.pop("external_contacts", None)
        # Model.save()'s own immutability guards raise a plain Django ValidationError -- left
        # to propagate uncaught, exactly like Correspondence's own serializer (LOT 3B) does
        # for the same kind of model-level guard.
        instance = super().update(instance, validated_data)
        if external_contacts is not None:
            instance.external_contacts.set(external_contacts)
        return instance
