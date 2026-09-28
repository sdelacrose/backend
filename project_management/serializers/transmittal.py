from django.db import transaction
from rest_framework import serializers

from project_management.models import Transmittal, TransmittalItem
from project_management.services.transmittal_reference import generate_transmittal_number


class TransmittalItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransmittalItem
        fields = [
            "id", "transmittal", "document_revision", "distribution_purpose",
            "evidence_snapshot", "created_at",
        ]
        read_only_fields = ["id", "transmittal", "evidence_snapshot", "created_at"]


class TransmittalSerializer(serializers.ModelSerializer):
    created_by = serializers.SerializerMethodField()
    items = TransmittalItemSerializer(many=True, read_only=True)

    class Meta:
        model = Transmittal
        fields = [
            "id", "transmittal_number", "direction", "status", "project",
            "external_company", "external_contacts", "subject", "message",
            "issued_at", "received_at", "evidence_snapshot", "items",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "issued_at", "received_at", "evidence_snapshot", "items",
            "created_by", "created_at", "updated_at",
        ]

    def get_created_by(self, obj):
        if not obj.created_by_id:
            return None
        return {"id": obj.created_by_id, "name": obj.created_by.full_name}

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
