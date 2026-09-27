from django.db import transaction
from rest_framework import serializers

from project_management.models import (
    Correspondence,
    ContactCompany,
    ContactUser,
    Project,
)
from project_management.services.correspondence_reference import (
    generate_correspondence_reference,
)
from project_management.services.correspondence_response import (
    apply_response_link,
    initial_response_status,
    validate_parent_correspondence,
    validate_status_for_direction,
)
from project_management.serializers.time_expense import get_valid_choices


def _owner_scope_for_user(user, project):
    """Mirrors Document/Folder's own scope resolution: an explicit `project` wins; otherwise
    the requester's own company (or, for a freelancer with no company, their personal
    library). Never trusts a client-supplied company_profile/freelancer directly."""
    if project is not None:
        return {"project": project, "company_profile": None, "freelancer": None}
    if user.is_freelancer and not getattr(user, "company_id", None):
        return {"project": None, "company_profile": None, "freelancer": user}
    return {"project": None, "company_profile": user.company, "freelancer": None}


class CorrespondenceSerializer(serializers.ModelSerializer):
    response_status = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = Correspondence
        fields = [
            "id", "reference", "direction", "status", "project", "company_profile", "freelancer",
            "correspondence_type", "subject", "message",
            "sender_company", "sender_contact", "recipient_company", "recipient_contact",
            "issue_date", "received_date",
            "response_required", "response_due_date", "response_status",
            "parent_correspondence",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "reference", "company_profile", "freelancer", "response_status",
            "created_by", "created_at", "updated_at",
        ]

    def get_response_status(self, obj):
        return obj.effective_response_status

    def get_created_by(self, obj):
        if not obj.created_by_id:
            return None
        return {"id": obj.created_by_id, "name": obj.created_by.full_name}

    # --- shared validation -----------------------------------------------------------

    def _validate_contact_belongs_to_company(self, company, contact, field):
        if contact is not None and contact.contact_company_id != (company.id if company else None):
            raise serializers.ValidationError(
                {field: "This contact does not belong to the selected company."}
            )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        direction = attrs.get("direction", instance.direction if instance else None)
        status = attrs.get("status", instance.status if instance else Correspondence.Status.ISSUED)
        validate_status_for_direction(direction, status)

        sender_company = attrs.get("sender_company", instance.sender_company if instance else None)
        sender_contact = attrs.get("sender_contact", instance.sender_contact if instance else None)
        recipient_company = attrs.get("recipient_company", instance.recipient_company if instance else None)
        recipient_contact = attrs.get("recipient_contact", instance.recipient_contact if instance else None)
        self._validate_contact_belongs_to_company(sender_company, sender_contact, "sender_contact")
        self._validate_contact_belongs_to_company(recipient_company, recipient_contact, "recipient_contact")

        if instance is None:
            request = self.context.get("request")
            user = request.user if request else self.context.get("user")
            scope = _owner_scope_for_user(user, attrs.get("project"))
            attrs["company_profile"] = scope["company_profile"]
            attrs["freelancer"] = scope["freelancer"]
        else:
            # LOT 3B section 10: reference/direction are guarded in Correspondence.save();
            # here we additionally protect the owner scope and the external parties once a
            # response relationship exists, since those changes would silently invalidate
            # historical sender/recipient/direction traceability.
            locked = instance.parent_correspondence_id or instance.responses.exists()
            if locked:
                for field in (
                    "project", "sender_company", "sender_contact",
                    "recipient_company", "recipient_contact",
                ):
                    if field in attrs and attrs[field] != getattr(instance, field):
                        raise serializers.ValidationError(
                            {field: "This cannot be changed once a response relationship exists."}
                        )
                if "response_required" in attrs and attrs["response_required"] != instance.response_required:
                    raise serializers.ValidationError(
                        {
                            "response_required": (
                                "response_required cannot be changed once a response "
                                "relationship exists."
                            )
                        }
                    )

        correspondence_type = attrs.get(
            "correspondence_type", instance.correspondence_type if instance else None
        )
        if correspondence_type:
            scope_project = attrs.get("project") if instance is None else instance.project
            scope_company = attrs.get("company_profile") if instance is None else instance.company_profile
            scope_freelancer = attrs.get("freelancer") if instance is None else instance.freelancer
            request = self.context.get("request")
            if scope_project is not None:
                valid_types = get_valid_choices("correspondence_types", scope_project.id, request)
            elif scope_company is not None:
                valid_types = get_valid_choices(
                    "correspondence_types", scope_company.id, request, is_company=True
                )
            elif scope_freelancer is not None:
                valid_types = get_valid_choices(
                    "correspondence_types", scope_freelancer.id, request, is_freelancer=True
                )
            else:
                from models import constants

                valid_types = constants.GC_CORRESPONDENCE_TYPES
            valid_types = {choice.split(":")[0] for choice in valid_types}
            if correspondence_type not in valid_types:
                raise serializers.ValidationError(
                    {
                        "correspondence_type": (
                            f"Invalid correspondence type. Allowed choices are: "
                            f"{', '.join(sorted(valid_types))}"
                        )
                    }
                )

        if "parent_correspondence" in attrs and attrs["parent_correspondence"] is not None:
            # Prefer the freshly-resolved scope (create) and fall back to the existing
            # instance (update) -- attrs["company_profile"]/["freelancer"] are only ever set
            # by the `instance is None` branch above.
            child_scope = {
                "project_id": attrs["project"].id if attrs.get("project") else (instance.project_id if instance else None),
                "company_profile_id": attrs["company_profile"].id if attrs.get("company_profile") else (instance.company_profile_id if instance else None),
                "freelancer_id": attrs["freelancer"].id if attrs.get("freelancer") else (instance.freelancer_id if instance else None),
            }
            validate_parent_correspondence(attrs["parent_correspondence"], direction, child_scope)

        return attrs

    # --- create / update ---------------------------------------------------------------

    def create(self, validated_data):
        request = self.context.get("request")
        user = request.user if request else self.context.get("user")
        response_required = validated_data.get("response_required", False)
        validated_data["response_status"] = initial_response_status(
            direction=validated_data["direction"], response_required=response_required
        )
        validated_data["created_by"] = user
        with transaction.atomic():
            validated_data["reference"] = generate_correspondence_reference(
                project=validated_data.get("project"),
                company_profile=validated_data.get("company_profile"),
                freelancer=validated_data.get("freelancer"),
            )
            instance = super().create(validated_data)
            apply_response_link(instance)
        return instance

    def update(self, instance, validated_data):
        validated_data.pop("reference", None)  # permanent: never accepted from a client anyway
        had_parent = instance.parent_correspondence_id
        had_status = instance.status
        if "response_required" in validated_data and not instance.responses.exists() and not instance.parent_correspondence_id:
            validated_data["response_status"] = initial_response_status(
                direction=validated_data.get("direction", instance.direction),
                response_required=validated_data["response_required"],
            )
        instance = super().update(instance, validated_data)
        # A newly-set parent, or a draft response finally being issued (DRAFT -> ISSUED), both
        # re-evaluate the parent's response_status (LOT 3B stabilization section 1).
        if instance.parent_correspondence_id and (
            instance.parent_correspondence_id != had_parent
            or (instance.status == Correspondence.Status.ISSUED and had_status == Correspondence.Status.DRAFT)
        ):
            apply_response_link(instance)
        return instance
