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
from project_management.services.correspondence_workflow import (
    is_content_locked,
    ready_to_send,
    validation_state,
)
from project_management.services.correspondence_send import resolve_recipient
from project_management.serializers.time_expense import get_valid_choices

# LOT 3D-3D1 section 8: fields locked once an OUT Correspondence's internal validation is
# active or approved -- "what is being approved" content, inspected field by field rather than
# copied from Document's own locking rule (Correspondence has different fields). Deliberately
# NOT locked: responsible, response_required, response_due_date (assignment/response-tracking
# metadata, not the content being validated).
_WORKFLOW_LOCKED_FIELDS = (
    "direction", "status", "reference", "external_reference",
    "project", "company_profile", "freelancer",
    "correspondence_type", "subject", "message",
    "sender_company", "sender_contact", "recipient_company", "recipient_contact",
    "issue_date", "received_date", "parent_correspondence",
)


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
    responsible_name = serializers.SerializerMethodField()
    # LOT 3D-3C0B: the smallest read-only addition needed for the drawer's compact "Related
    # Correspondences" section (View mode) -- reuses the existing parent_correspondence FK and
    # its `responses` reverse relation, never a second relationship/model.
    parent_reference = serializers.SerializerMethodField()
    responses = serializers.SerializerMethodField()
    # LOT 3D-3D1: the ONE derived validation state (section 5) and the ONE canonical
    # can_send/ready_to_send rule (section 15) -- both computed server-side, never re-encoded
    # on the frontend. Always None/False for IN (validation applies to OUT only, section 2).
    validation_state = serializers.SerializerMethodField()
    ready_to_send = serializers.SerializerMethodField()
    content_locked = serializers.SerializerMethodField()
    # LOT 3D-3D2 section 4: the exact {email, name} SEND will use -- reuses
    # services.correspondence_send.resolve_recipient rather than re-deriving "which Network
    # field is the TO address" on the frontend. None whenever no usable recipient email exists
    # (never a guessed company-generic address).
    resolved_recipient = serializers.SerializerMethodField()

    class Meta:
        model = Correspondence
        fields = [
            "id", "reference", "external_reference", "direction", "status",
            "project", "company_profile", "freelancer",
            "correspondence_type", "subject", "message",
            "sender_company", "sender_contact", "recipient_company", "recipient_contact",
            "issue_date", "received_date",
            "response_required", "response_due_date", "response_status",
            "parent_correspondence", "parent_reference", "responses",
            "responsible", "responsible_name",
            "validation_state", "ready_to_send", "content_locked", "resolved_recipient",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "reference", "company_profile", "freelancer", "response_status",
            "responsible_name", "parent_reference", "responses",
            "validation_state", "ready_to_send", "content_locked", "resolved_recipient",
            "created_by", "created_at", "updated_at",
        ]

    def get_response_status(self, obj):
        return obj.effective_response_status

    def get_created_by(self, obj):
        if not obj.created_by_id:
            return None
        return {"id": obj.created_by_id, "name": obj.created_by.full_name}

    def get_responsible_name(self, obj):
        if not obj.responsible_id:
            return None
        return obj.responsible.full_name

    def get_parent_reference(self, obj):
        if not obj.parent_correspondence_id:
            return None
        # LOT 3D-3C0C section 8: the direction-aware display reference (never the raw
        # internally-generated `reference` alone) -- an IN parent with no generated reference
        # falls back to its own display_reference logic rather than surfacing blank/undefined.
        return obj.parent_correspondence.display_reference or None

    def get_validation_state(self, obj):
        if obj.pk is None:
            return None
        return validation_state(obj)

    def get_ready_to_send(self, obj):
        if obj.pk is None:
            return False
        return ready_to_send(obj)

    def get_content_locked(self, obj):
        if obj.pk is None:
            return False
        return is_content_locked(obj)

    def get_resolved_recipient(self, obj):
        if obj.pk is None or obj.direction != Correspondence.Direction.OUT:
            return None
        return resolve_recipient(obj)

    def get_responses(self, obj):
        if obj.pk is None:
            return []
        return [
            {
                "id": response.id,
                "reference": response.display_reference,
                "direction": response.direction,
                "status": response.status,
            }
            for response in obj.responses.all().order_by("-created_at")
        ]

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

        # LOT 3D-3D1 section 8/14: content is locked server-side while an internal validation
        # workflow is active or approved -- frontend disabling alone is never sufficient.
        if instance is not None and is_content_locked(instance):
            # Compares the VALUE, not mere presence: the frontend always resends every field on
            # save (not a diff), so presence alone would wrongly reject e.g. a responsible-only
            # change that happens to resend an unchanged subject/direction/etc alongside it.
            locked_fields_changed = [
                field for field in _WORKFLOW_LOCKED_FIELDS
                if field in attrs and attrs[field] != getattr(instance, field)
            ]
            if locked_fields_changed:
                raise serializers.ValidationError(
                    {
                        field: (
                            "This cannot be changed while an internal validation workflow "
                            "is active or approved."
                        )
                        for field in locked_fields_changed
                    }
                )

        sender_company = attrs.get("sender_company", instance.sender_company if instance else None)
        sender_contact = attrs.get("sender_contact", instance.sender_contact if instance else None)
        recipient_company = attrs.get("recipient_company", instance.recipient_company if instance else None)
        recipient_contact = attrs.get("recipient_contact", instance.recipient_contact if instance else None)
        self._validate_contact_belongs_to_company(sender_company, sender_contact, "sender_contact")
        self._validate_contact_belongs_to_company(recipient_company, recipient_contact, "recipient_contact")

        # LOT 3D-3D1 pre-flight B: a NEW IN never gets an internally generated reference (LOT
        # 3D-3C0C section 6/7) -- external_reference is its only meaningful reference, so it is
        # now required going forward. Historical IN records that predate this rule (blank
        # external_reference, no DB constraint added -- no destructive migration) stay freely
        # editable on every OTHER field; this only rejects a create or an update that itself
        # would leave external_reference blank on an IN, never merely for existing that way.
        if direction == Correspondence.Direction.IN:
            new_external_reference = attrs.get(
                "external_reference", instance.external_reference if instance else None
            )
            reference_being_set = instance is None or "external_reference" in attrs
            if reference_being_set and not (new_external_reference or "").strip():
                raise serializers.ValidationError(
                    {
                        "external_reference": (
                            "The sender's own reference is required for an incoming "
                            "correspondence."
                        )
                    }
                )

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
            validate_parent_correspondence(
                attrs["parent_correspondence"], direction, child_scope,
                exclude_child_id=instance.id if instance else None,
            )

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
            # LOT 3D-3C0C section 6/7: only an OUTGOING Correspondence gets an internally
            # generated LINKS reference (and consumes a CorrespondenceReferenceCounter
            # sequence) -- an IN has already been received from outside, so a system-minted
            # number for it has no meaning; its `reference` stays blank (the model's own
            # default) and the sender's own external_reference (already a plain writable field
            # on this serializer) is what the user enters manually instead. Never touched here:
            # historical rows, already generated before this lot, are never rewritten.
            if validated_data["direction"] == Correspondence.Direction.OUT:
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
