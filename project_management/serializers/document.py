from project_management.models.constants import COLLABORATIVE_SOLUTION
from project_management.serializers.time_expense import get_valid_choices
from rest_framework import serializers
from django.conf import settings
from django.db import transaction

from models.models import CompanyConfig, CompanyProfile
from models.hierarchical_configurations import (
    DOCUMENT_DISCIPLINES_KEY,
    resolve_nested_configurations,
)
from project_management.models import Document, DocumentOrigin, DocumentRevision, DocumentSearchIndex, Folder, Project
from project_management.utils import get_product_name_from_request, has_project_permission, get_abbreviation
from project_management.models import ECPMPConfig
from project_management.services.document_reference import generate_document_reference
from project_management.services.document_workflow import (
    document_has_workflow,
    get_internal_status,
)
from project_management.serializers.file_name import validate_display_file_name
from project_management.services.document_numbering import (
    resolve_document_library_scope,
)

class DocumentHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Document.history.model
        fields = [
            'id',
            'history_date',
            'history_change_reason',
            'history_type',
            'history_user',
            'file_name',
            'document_type',
            'identification_number',
            'revision',
            'document_origin',
            'status',
            'internal_review_status',
            'external_review_status',
            'transmittal_category',
            'author'
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        
        if instance.history_user:
            representation['history_user'] = {
                "id": instance.history_user.id,
                "name": instance.history_user.full_name
            }
        
        if instance.author:
            representation['author'] = {
                "id": instance.author.id,
                "name": instance.author.full_name
            }
        
        return representation

class DocumentSerializer(serializers.ModelSerializer):
    history = serializers.SerializerMethodField()
    linked_items = serializers.SerializerMethodField()
    index_status = serializers.SerializerMethodField()
    project = serializers.SerializerMethodField()
    creator = serializers.SerializerMethodField()
    approver = serializers.SerializerMethodField()
    file_size = serializers.SerializerMethodField()
    # Blank entries are accepted here and dropped by validate_tags.
    tags = serializers.ListField(
        child=serializers.CharField(allow_blank=True, max_length=100),
        required=False,
        allow_empty=True,
    )
    workflow = serializers.SerializerMethodField()
    internal_status = serializers.SerializerMethodField()
    revision_label = serializers.SerializerMethodField()
    # D13: the external issuer's own revision designation of the CURRENT version (Supplier,
    # potentially Client) -- distinct from revision_label (INTERNAL-origin, approval-only).
    current_external_revision = serializers.SerializerMethodField()
    origin_locked = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            'id',
            'folder',
            'author',
            'file',
            'file_name',
            'document_type',
            'document_origin',
            'external_issuer_company',
            'origin_locked',
            'status',
            'internal_review_status',
            'external_review_status',
            'transmittal_category',
            'uploaded_at',
            'updated_at',
            'identification_number',
            'client_reference',
            'category',
            'discipline',
            'discipline_group',
            'discipline_group_id',
            'discipline_option_id',
            'discipline_code',
            'sequence_number',
            'revision',
            'history',
            'linked_items',
            'full_document_number',
            'index_status',
            'project',
            'creator',
            'approver',
            'workflow',
            'internal_status',
            'revision_label',
            'current_external_revision',
            'file_size',
            'description',
            'tags',
        ]
        read_only_fields = ['full_document_number', 'sequence_number', 'created_by', 'assignee']

    def validate_tags(self, value):
        # Trim, drop empty values and exact duplicates; capitalization is preserved.
        seen = []
        for tag in value or []:
            tag = str(tag).strip()
            if tag and tag not in seen:
                seen.append(tag)
        return seen

    def validate_revision(self, value):
        """
        Validate that the revision is an integer between 0 and 99 (inclusive).
        """
        if value is None:
            return value
        if not isinstance(value, int):
            raise serializers.ValidationError("Revision number must be an integer.")
        if value < 0 or value > 99:
            raise serializers.ValidationError("Revision number must be between 0 and 99.")
        return value

    def validate_file_name(self, value):
        incoming_file = self.initial_data.get('file') if hasattr(self, 'initial_data') else None
        current_file_name = self.instance.file_name if self.instance else None
        source_file = incoming_file or (self.instance.file if self.instance else None)
        return validate_display_file_name(
            value,
            source_file=source_file,
            current_file_name=current_file_name,
        )

    def validate_folder(self, value):
        request_user = self.context.get('user')
        if value.project:
            request = self.context.get("request")
            product_name = get_product_name_from_request(request)
            if product_name in COLLABORATIVE_SOLUTION:
                products = COLLABORATIVE_SOLUTION
            else:
                products = [product_name]
            if value.project.user == request_user and value.project.solution in products:
                return value
            return value
            # TODO: Adjust as per new permission system
            # if has_project_permission("PUT", request_user, value.project, product_name):
            #     return value
        if value.company_profile and value.company_profile == request_user.company:
            return value
        
        if value.freelancer and value.freelancer == request_user:
            return value

        raise serializers.ValidationError(f"Folder {value} does not belong to this user or you don't have access to this project/company.")

    def get_project_freelancer_and_company_from_folder(self, folder):
        project_id = None
        company_id = None
        freelancer_id = None
        if folder.project:
            project_id = folder.project.id
        elif folder.company_profile:
            company_id = folder.company_profile_id
        elif folder.freelancer:
            freelancer_id = folder.freelancer.id
        return project_id, company_id, freelancer_id

    def get_project_id_and_company_id(self):
        project_id = None
        company_id = None
        freelancer_id = None
        if self.instance:
            folder = self.instance.folder
            project_id, company_id, freelancer_id = self.get_project_freelancer_and_company_from_folder(folder)
        else:
            folder_id = self.initial_data.get('folder')
            folder_instance = Folder.objects.filter(id=folder_id).first()
            if not folder_instance:
                raise serializers.ValidationError("Invalid folder specified.")
            project_id, company_id, freelancer_id = self.get_project_freelancer_and_company_from_folder(folder_instance)
        return project_id, company_id, freelancer_id

    def _is_unchanged_value(self, field_name, value):
        return self.instance is not None and getattr(
            self.instance, field_name, None
        ) == value

    def _get_valid_choices_for_config_key(self, config_key):
        request = self.context.get('request')
        project_id, company_id, freelancer_id = self.get_project_id_and_company_id()
        if project_id:
            return get_valid_choices(config_key, project_id, request)
        if company_id:
            return get_valid_choices(config_key, company_id, request, is_company=True)
        if freelancer_id:
            return get_valid_choices(config_key, freelancer_id, request, is_freelancer=True)
        return []

    def _validate_optional_choice_field(
        self, value, config_key, error_label, field_name
    ):
        if value in (None, ""):
            return value

        valid_choices = self._get_valid_choices_for_config_key(config_key)
        valid_values = {choice.split(":")[0] for choice in valid_choices}
        if (
            valid_values
            and value not in valid_values
            and not self._is_unchanged_value(field_name, value)
        ):
            raise serializers.ValidationError(
                f"Invalid {error_label}. Allowed choices are: {', '.join(valid_values)}"
            )
        return value

    def validate_status(self, value):
        return self._validate_optional_choice_field(
            value, "document_status", "document status", "status"
        )

    def validate_internal_review_status(self, value):
        return self._validate_optional_choice_field(
            value,
            "document_internal_review_status",
            "document internal review status",
            "internal_review_status",
        )

    def validate_external_review_status(self, value):
        return self._validate_optional_choice_field(
            value,
            "document_external_review_status",
            "document external review status",
            "external_review_status",
        )

    def validate_transmittal_category(self, value):
        return self._validate_optional_choice_field(
            value,
            "transmittal_category",
            "transmittal category",
            "transmittal_category",
        )

    def validate_author(self, value):
        """
        Validate that the author is a valid team member and belongs to the same project as the folder.
        """
        request_user = self.context.get('user')
        folder_instance = self.instance.folder if self.instance else None
        if hasattr(self, 'initial_data') and 'folder' in self.initial_data:
            folder_id = self.initial_data.get('folder')
            folder_instance = Folder.objects.filter(id=folder_id).select_related('project').first()
        
        if not folder_instance:
            raise serializers.ValidationError("Invalid folder specified.")

        if not value:
            raise serializers.ValidationError("Author cannot be null.")

        # Check if the author belongs to the same project/company as the folder
        project_id, company_id, freelancer_id = self.get_project_freelancer_and_company_from_folder(folder_instance)
        
        # TODO: Adjust as per new permission system
        # if project_id:
        #     if not has_project_permission("PUT", value, folder_instance.project, get_product_name_from_request(self.context.get('request'))):
        #         raise serializers.ValidationError("The author must belong to the same project as the folder.")
        if project_id:
            return value
        if company_id:
            if value.is_freelancer or folder_instance.company_profile != value.company:
                raise serializers.ValidationError("The author must belong to the same company as the folder.")
        elif freelancer_id:
            if not request_user.is_freelancer or folder_instance.freelancer != value or value != request_user:
                raise serializers.ValidationError("The author must be the same freelancer as the folder.")
        else:
            raise serializers.ValidationError("The related folder must belong to a company/project.")

        return value
    

    def validate_document_type(self, value):
        if not value:
            return value
        
        request = self.context.get('request')
        project_id, company_id, freelancer_id = self.get_project_id_and_company_id()

        if project_id:
            valid_choices = get_valid_choices("document_types", project_id, request)
        elif company_id:
            valid_choices = get_valid_choices("document_types", company_id, request, is_company=True)
        elif freelancer_id:
            valid_choices = get_valid_choices("document_types", freelancer_id, request, is_freelancer=True)
        else:
            from models import constants
            valid_choices = constants.GC_COMPANY_DOCUMENT_TYPES
        valid_choices = {choice.split(":")[0] for choice in valid_choices}
        if value not in valid_choices and not self._is_unchanged_value(
            "document_type", value
        ):
            raise serializers.ValidationError(f"Invalid document type. Allowed choices are: {', '.join(valid_choices)}")
        
        return value

    def validate_category(self, value):
        if not value:
            return value
        
        request = self.context.get('request')
        project_id, company_id, freelancer_id = self.get_project_id_and_company_id()
        
        if project_id:
            valid_choices = get_valid_choices("document_categories", project_id, request)
        elif company_id:
            valid_choices = get_valid_choices("document_categories", company_id, request, is_company=True)
        elif freelancer_id:
            valid_choices = get_valid_choices("document_categories", freelancer_id, request, is_freelancer=True)
        else:
            from project_management.models.constants import GC_DOCUMENT_CATEGORIES
            valid_choices = GC_DOCUMENT_CATEGORIES
        valid_choices = {choice.split(":")[0] for choice in valid_choices}
        if value not in valid_choices and not self._is_unchanged_value(
            "category", value
        ):
            raise serializers.ValidationError(f"Invalid document category. Allowed choices are: {', '.join(valid_choices)}")
        
        return value

    def validate_discipline(self, value):
        if not value:
            return value

        # Hierarchical submissions are validated against the selected group and
        # option IDs in validate(), including company-defined custom options.
        initial_data = getattr(self, "initial_data", {})
        if initial_data.get("discipline_option_id"):
            return value
        
        request = self.context.get('request')
        project_id, company_id, freelancer_id = self.get_project_id_and_company_id()
        
        if project_id:
            valid_choices = get_valid_choices("document_disciplines", project_id, request)
        elif company_id:
            valid_choices = get_valid_choices("document_disciplines", company_id, request, is_company=True)
        elif freelancer_id:
            valid_choices = get_valid_choices("document_disciplines", freelancer_id, request, is_freelancer=True)
        else:
            from project_management.models.constants import GC_DOCUMENT_DISCIPLINES
            valid_choices = GC_DOCUMENT_DISCIPLINES
        valid_choices = {choice.split(":")[0] for choice in valid_choices}
        if value not in valid_choices and not self._is_unchanged_value(
            "discipline", value
        ):
            raise serializers.ValidationError(f"Invalid discipline. Allowed choices are: {', '.join(valid_choices)}")
        
        return value

    @staticmethod
    def _issuer_in_scope(issuer, folder):
        """The issuer must be a live Network company of the library owner (no cross-tenant use)."""
        if issuer.is_deleted:
            return False
        if folder.company_profile_id:
            return issuer.company_profile_id == folder.company_profile_id
        if folder.freelancer_id:
            return issuer.user_id == folder.freelancer_id
        owner = folder.project.user if folder.project_id else None
        if owner is None:
            return False
        if getattr(owner, "company_id", None):
            return issuer.company_profile_id == owner.company_id
        return issuer.user_id == owner.id

    def _validate_origin(self, attrs):
        """Document Origin rules (V1): explicit origin, issuer only for CLIENT / SUPPLIER."""
        instance = self.instance
        origin_sent = "document_origin" in attrs
        issuer_sent = "external_issuer_company" in attrs
        if not origin_sent and not issuer_sent:
            return attrs

        current_origin = instance.document_origin if instance else DocumentOrigin.INTERNAL
        origin = attrs.get("document_origin", current_origin)

        # LOT 3D-3D1 pre-flight A: a Company Document is never a Project deliverable and is
        # never distributed via Transmittal (LOT 3D-3C0) -- its origin is always INTERNAL,
        # immutably, and this was previously enforced by the frontend only (it always sends
        # INTERNAL for a Company Document and never offers another choice), leaving a client
        # that bypasses the UI free to submit CLIENT/SUPPLIER directly. Enforced here too now,
        # regardless of workflow state, since it is a scope rule, not a workflow-lock rule.
        origin_folder = attrs.get("folder") or (instance.folder if instance else None)
        if origin != DocumentOrigin.INTERNAL and origin_folder is not None and origin_folder.company_profile_id:
            raise serializers.ValidationError(
                {"document_origin": "A Company Document's origin is always Internal."}
            )

        if (
            instance
            and origin_sent
            and origin != current_origin
            and document_has_workflow(instance)
        ):
            # Internal approval history must never be silently reclassified.
            raise serializers.ValidationError(
                {
                    "document_origin": (
                        "The origin cannot be changed once an internal validation "
                        "workflow exists for this document."
                    )
                }
            )

        if origin == DocumentOrigin.INTERNAL:
            # An issuer submitted for an internal document is cleaned up, never stored.
            attrs["external_issuer_company"] = None
            return attrs

        issuer = (
            attrs["external_issuer_company"]
            if issuer_sent
            else (instance.external_issuer_company if instance else None)
        )
        if issuer is None:
            if issuer_sent or origin_sent:
                raise serializers.ValidationError(
                    {
                        "external_issuer_company": (
                            "The issuing company is required for client and supplier documents."
                        )
                    }
                )
            return attrs
        folder = attrs.get("folder") or (instance.folder if instance else None)
        unchanged = instance is not None and instance.external_issuer_company_id == issuer.id
        if folder is not None and not unchanged and not self._issuer_in_scope(issuer, folder):
            raise serializers.ValidationError(
                {
                    "external_issuer_company": (
                        "This company does not belong to the network of the document library."
                    )
                }
            )
        return attrs

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs = self._validate_origin(attrs)

        if (
            self.instance is None
            and hasattr(self, "initial_data")
            and not attrs.get("file")
        ):
            # Register entry without a main file (TO_CREATE): the title is the file_name.
            if not str(attrs.get("file_name") or "").strip():
                raise serializers.ValidationError(
                    {"file_name": "A title is required when no file is provided."}
                )

        status = attrs.get("status")
        internal_review_status = attrs.get("internal_review_status")
        external_review_status = attrs.get("external_review_status")
        if self.instance:
            if "status" not in attrs:
                status = self.instance.status
            if "internal_review_status" not in attrs:
                internal_review_status = self.instance.internal_review_status
            if "external_review_status" not in attrs:
                external_review_status = self.instance.external_review_status

        if status == "Submitted" and not (
            internal_review_status or external_review_status
        ):
            raise serializers.ValidationError(
                "As the document is submitted, please add a review status."
            )

        discipline_fields = {
            "discipline",
            "discipline_group_id",
            "discipline_option_id",
        }
        initial_data = getattr(self, "initial_data", attrs)
        if not discipline_fields.intersection(initial_data.keys()):
            return attrs

        requested_group_id = attrs.get("discipline_group_id")
        requested_option_id = attrs.get("discipline_option_id")
        requested_label = attrs.get("discipline")

        if not requested_group_id and not requested_option_id and not requested_label:
            attrs["discipline_group"] = ""
            attrs["discipline_group_id"] = ""
            attrs["discipline_option_id"] = ""
            attrs["discipline_code"] = ""
            return attrs

        if requested_group_id and not requested_option_id and not requested_label:
            raise serializers.ValidationError(
                {
                    "discipline_option_id": (
                        "A discipline is required for the selected group."
                    )
                }
            )

        config = self.get_config()
        if not config:
            return attrs
        hierarchy = resolve_nested_configurations(
            config.nested_configurations
        )[DOCUMENT_DISCIPLINES_KEY]

        match = None
        for group in hierarchy["groups"]:
            for child in group["children"]:
                option_matches = (
                    requested_option_id and child["id"] == requested_option_id
                )
                label_matches = requested_label and child["label"] == requested_label
                if option_matches or (not requested_option_id and label_matches):
                    match = (group, child)
                    break
            if match:
                break

        if not match:
            if requested_option_id:
                raise serializers.ValidationError(
                    {"discipline_option_id": "Invalid document discipline option."}
                )
            return attrs

        group, child = match
        if requested_group_id and requested_group_id != group["id"]:
            raise serializers.ValidationError(
                {
                    "discipline_option_id": (
                        "The discipline does not belong to the selected group."
                    )
                }
            )

        attrs["discipline"] = child["label"]
        attrs["discipline_group"] = group["label"]
        attrs["discipline_group_id"] = group["id"]
        attrs["discipline_option_id"] = child["id"]
        attrs["discipline_code"] = child.get("code", "")
        return attrs

    def get_config(self):
        request = self.context.get('request')
        project_id, company_id, freelancer_id = self.get_project_id_and_company_id()
        product_name = get_product_name_from_request(request) if request else "Energy Consultant Solution"
        
        if company_id:
            config = CompanyConfig.objects.filter(
                company_profile=company_id,
                solution=product_name
            ).first()
        elif freelancer_id:
            config = CompanyConfig.objects.filter(
                user=freelancer_id, 
                solution=product_name
            ).first()
        else:
            config = ECPMPConfig.objects.filter(project_id=project_id).first()
        
        return config

    def create(self, validated_data):
        # Ensure author is set to the current user if not provided
        request_user = self.context.get('user')
        config = self.get_config()
        if not validated_data.get('author'):
            validated_data['author'] = request_user

        with transaction.atomic():
            # D13 live review section 8: the Internal Ref. (full_document_number) is a
            # LINKS/company-controlled reference -- it must never be forced onto a document the
            # company did not itself originate. Only INTERNAL-origin documents (including legacy
            # documents, whose default is INTERNAL) get one; SUPPLIER/CLIENT documents keep both
            # fields blank unless a future workflow explicitly assigns one. Permanent once
            # generated -- never recalculated afterwards.
            origin = validated_data.get('document_origin') or DocumentOrigin.INTERNAL
            if origin == DocumentOrigin.INTERNAL:
                (
                    validated_data['sequence_number'],
                    validated_data['full_document_number'],
                ) = generate_document_reference(
                    validated_data['folder'],
                    document_type=validated_data.get('document_type'),
                    discipline=validated_data.get('discipline'),
                    discipline_code=validated_data.get('discipline_code'),
                    category=validated_data.get('category'),
                    config=config,
                )
            validated_data['created_by'] = request_user
            validated_data['assignee'] = validated_data.get('author')
            instance = super().create(validated_data)
            return instance

    def update(self, instance, validated_data):
        if 'file' in validated_data:
            # The controlled main file only changes through a new version (revision endpoint);
            # an ordinary update can never silently replace it.
            raise serializers.ValidationError(
                {
                    'file': (
                        'The main file cannot be replaced by updating the document. '
                        'Upload it as a new version (POST /document/<id>/revisions).'
                    )
                }
            )
        next_folder = validated_data.get('folder')
        if (
            next_folder is not None
            and next_folder.pk != instance.folder_id
            and resolve_document_library_scope(instance.folder)
            != resolve_document_library_scope(next_folder)
        ):
            raise serializers.ValidationError(
                {
                    'folder': (
                        'Documents cannot be moved between company, project, '
                        'or freelancer libraries.'
                    )
                }
            )
        # The document reference (full_document_number) is generated ONCE at creation and is
        # permanent: no metadata, version, revision or legacy Document.revision change ever
        # recalculates it (it is also read-only through the API). A legacy blank reference
        # stays blank rather than being invented from partial data on an unrelated update.
        validated_data['sequence_number'] = instance.sequence_number
        validated_data.pop('full_document_number', None)
        # Use user-provided revision or keep existing revision
        if 'revision' not in validated_data:
            validated_data['revision'] = instance.revision
        validated_data['assignee'] = instance.author if "author" not in validated_data else validated_data.get('author')
        return super().update(instance, validated_data)

    def get_history(self, obj):
        include_history = self.context.get('include_history', False)
        if not include_history:
            return None
            
        history = obj.history.all().order_by('-history_date')
        return DocumentHistorySerializer(history, many=True).data

    def get_project(self, obj):
        # Only computed where the caller opted in (Documents list/detail), to avoid
        # extra queries when this serializer is nested elsewhere (e.g. folder trees).
        if not self.context.get('include_control'):
            return None
        project = obj.folder.project if obj.folder_id else None
        if project is None:
            return None
        return {'id': project.id, 'name': project.project_name}

    @staticmethod
    def _user_ref(user):
        if user is None:
            return None
        return {'id': user.id, 'name': (user.full_name or '').strip() or user.email}

    def get_creator(self, obj):
        if not self.context.get('include_control'):
            return None
        return self._user_ref(obj.created_by)

    def get_approver(self, obj):
        # Optional Document Control data (DocumentControl is created on first save only).
        if not self.context.get('include_control'):
            return None
        control = getattr(obj, 'control', None)
        return self._user_ref(control.approver) if control else None

    def get_workflow(self, obj):
        # Register indicator only: status of the CURRENT revision's workflow and whether the
        # connected user is the expected actor. Annotated by the list queryset; no events.
        if not self.context.get('include_control'):
            return None
        status_value = getattr(obj, 'wf_status', None)
        if not status_value:
            return None
        user = self.context.get('user')
        expected = getattr(obj, 'wf_expected_actor_id', None)
        return {
            'status': status_value,
            'awaiting_me': bool(expected and user and expected == user.id),
        }

    def get_revision_label(self, obj):
        # Canonical official Revision of the CURRENT version (null until approved); never the
        # legacy Document.revision. Annotated by the list queryset, one lookup otherwise.
        if not self.context.get('include_control'):
            return None
        if hasattr(obj, 'current_revision_label'):
            return obj.current_revision_label
        current = DocumentRevision.objects.filter(document=obj).order_by(
            '-created_at', '-id'
        ).first()
        return current.revision_label if current else None

    def get_current_external_revision(self, obj):
        # D13: the external issuer's own revision designation of the CURRENT version -- same
        # include_control gating and annotation-first pattern as get_revision_label above.
        if not self.context.get('include_control'):
            return None
        if hasattr(obj, 'current_external_revision'):
            return obj.current_external_revision
        current = DocumentRevision.objects.filter(document=obj).order_by(
            '-created_at', '-id'
        ).first()
        return current.external_revision if current else None

    def get_origin_locked(self, obj):
        # True once an internal workflow (active or historical) exists: the origin is then
        # immutable. Annotated by the list queryset; one lookup on the detail.
        if not self.context.get('include_control'):
            return None
        if hasattr(obj, 'has_workflow_history'):
            return bool(obj.has_workflow_history)
        return document_has_workflow(obj)

    def get_internal_status(self, obj):
        # Derived (never stored) validation lifecycle of the CURRENT version:
        # DRAFT / IN_VALIDATION / APPROVED. Independent from the business Document.status.
        if not self.context.get('include_control'):
            return None
        return get_internal_status(obj)

    def get_file_size(self, obj):
        # Detail page only: may hit the storage backend.
        if not self.context.get('include_detail') or not obj.file:
            return None
        try:
            return obj.file.size
        except Exception:
            return None

    def get_index_status(self, obj):
        try:
            return obj.search_index.status
        except DocumentSearchIndex.DoesNotExist:
            return "not_indexed"

    def get_linked_items(self, obj):
        """
        Returns all items linked to this document including:
        - Action Items
        - Milestones
        - Object Assessments
        - Project Funding
        - Billing
        - Invoices
        - Opportunities
        - Calendar Events
        """
        include_linked_items = self.context.get('include_linked_items', True)
        if not include_linked_items:
            return None
            
        linked_items = {
            'action_items': [],
            'milestones': [],
            'object_assessments': [],
            'project_funding': [],
            'invoices': [],
            'opportunities': [],
            'calendar_events': []
        }
        
        # Get action items
        if obj.action_items.exists():
            linked_items['action_items'] = [
                {
                    'id': item.id,
                    'name': item.name,
                    'status': item.status,
                    'milestone': {
                        'id': item.milestone.id,
                        'name': item.milestone.name
                    } if item.milestone else None,
                    'created_by': {
                        'id': item.created_by.id,
                        'name': item.created_by.full_name
                    } if item.created_by else None,
                    'assignees': [
                        {
                            'id': assignee.id,
                            'name': assignee.full_name
                        }
                        for assignee in item.assignees.all()
                    ],
                    'company_profile': {
                        'id': item.company_profile.id,
                        'name': item.company_profile.company_name
                    } if item.company_profile else None,
                    'freelancer': {
                        'id': item.freelancer.id,
                        'name': item.freelancer.full_name
                    } if item.freelancer else None
                }
                for item in obj.action_items.all()
            ]
        
        # Get milestones
        if obj.milestones.exists():
            linked_items['milestones'] = [
                {
                    'id': milestone.id,
                    'name': milestone.name,
                    'status': milestone.status,
                    'due_date': milestone.due_date,
                    'project': {
                        'id': milestone.project.id,
                        'name': milestone.project.project_name
                    } if milestone.project else None
                }
                for milestone in obj.milestones.all()
            ]
        
        # Get object assessments
        if obj.object_assesments.exists():
            linked_items['object_assessments'] = [
                {
                    'id': assessment.id,
                    'record_title': assessment.record_title,
                    'record_type': assessment.record_type,
                    'project': {
                        'id': assessment.project.id,
                        'name': assessment.project.project_name
                    } if assessment.project else None
                }
                for assessment in obj.object_assesments.all()
            ]
        
        # Get project funding
        if obj.fundings.exists():
            linked_items['project_funding'] = [
                {
                    'id': funding.id,
                    'name': funding.name,
                    'budget': funding.budget,
                    'created_by': {
                        'id': funding.created_by.id,
                        'name': funding.created_by.full_name
                    } if funding.created_by else None,
                    'project': {
                        'id': funding.project.id,
                        'name': funding.project.project_name
                    } if funding.project else None
                }
                for funding in obj.fundings.all()
            ]
        
        # Get invoices
        if obj.billing_invoices.exists():
            linked_items['invoices'] = [
                {
                    'id': invoice.id,
                    'reference_number': invoice.reference_number,
                    'document_type': invoice.document_type,
                    'invoice_date': invoice.invoice_date,
                    'due_date': invoice.due_date,
                    'status': invoice.status,
                    'grand_total': invoice.grand_total,
                    'project': {
                        'id': invoice.project.id,
                        'name': invoice.project.project_name
                    } if invoice.project else None,
                    'company_profile': {
                        'id': invoice.company_profile.id,
                        'name': invoice.company_profile.company_name
                    } if invoice.company_profile else None,
                    'freelancer': {
                        'id': invoice.freelancer.id,
                        'name': invoice.freelancer.full_name
                    } if invoice.freelancer else None
                }
                for invoice in obj.billing_invoices.all()
            ]
        
        # Get opportunities
        if obj.opportunities.exists():
            linked_items['opportunities'] = [
                {
                    'id': opportunity.id,
                    'title': opportunity.title,
                    'created_by': {
                        'id': opportunity.created_by.id,
                        'name': opportunity.created_by.full_name
                    } if opportunity.created_by else None,
                    'company_profile': {
                        'id': opportunity.company_profile.id,
                        'name': opportunity.company_profile.company_name
                    } if opportunity.company_profile else None,
                    'freelancer': {
                        'id': opportunity.freelancer.id,
                        'name': opportunity.freelancer.full_name
                    } if opportunity.freelancer else None
                }
                for opportunity in obj.opportunities.all()
            ]
        
        # Get calendar events
        if obj.attachments.exists():
            linked_items['calendar_events'] = [
                {
                    'id': event.id,
                    'name': event.name,
                    'start_date': event.start_date,
                    'end_date': event.end_date,
                    'user': {
                        'id': event.user.id,
                        'name': event.user.full_name
                    } if event.user else None,
                    'company_profile': {
                        'id': event.company_profile.id,
                        'name': event.company_profile.company_name
                    } if event.company_profile else None,
                    'freelancer': {
                        'id': event.freelancer.id,
                        'name': event.freelancer.full_name
                    } if event.freelancer else None,
                    'assignees': [
                        {
                            'id': assignee.id,
                            'name': assignee.full_name
                        } for assignee in event.assignees.all()
                    ]
                }
                for event in obj.attachments.all()
            ]
        
        return linked_items

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        
        representation['revision'] = instance.revision if instance.revision is not None else 0
        
        if instance.author:
            representation['author'] = {
                "id": instance.author.id,
                "name": instance.author.full_name
            }
        if instance.created_by:
            representation['created_by'] = {
                "id": instance.created_by.id,
                "name": instance.created_by.full_name
            }
        
        if instance.assignee:
            representation['assignee'] = {
                "id": instance.assignee.id,
                "name": instance.assignee.full_name
            }
        if instance.folder:
            representation["folder"]={
                "id" : instance.folder.id,
                "name": instance.folder.name
            }
        issuer = instance.external_issuer_company
        representation["external_issuer_company"] = (
            {"id": issuer.id, "name": issuer.company_name} if issuer else None
        )
        return representation

class FolderSerializer(serializers.ModelSerializer):
    documents = serializers.SerializerMethodField()
    subfolders = serializers.SerializerMethodField()

    class Meta:
        model = Folder
        fields = ['id', 'project', 'company_profile', 'freelancer', 'name', 'system_key', 'parent', 'created_at', 'updated_at', 'documents', 'subfolders']
        read_only_fields = ['system_key']

    def get_subfolders(self, obj):
        # Serialize subfolders using the same serializer
        subfolders = Folder.objects.filter(parent=obj)
        return FolderSerializer(subfolders, many=True, context=self.context).data

    def get_documents(self, obj):
        return DocumentSerializer(obj.documents.all(), many=True, context=self.context).data

    def validate(self, attrs):
        if self.instance and self.instance.system_key:
            protected_fields = {
                "name",
                "parent",
                "project",
                "company_profile",
                "freelancer",
            }
            changed_fields = {
                field
                for field in protected_fields.intersection(attrs)
                if getattr(self.instance, f"{field}_id", getattr(self.instance, field, None))
                != (
                    getattr(attrs[field], "id", attrs[field])
                    if field != "name"
                    else attrs[field]
                )
            }
            if changed_fields:
                raise serializers.ValidationError(
                    "System folders cannot be renamed, moved, or repurposed."
                )
        return attrs

    # TODO: Adjust as per new permission system
    # def validate_project(self, value):
    #     if value is None:
    #         return value
    #     request_user = self.context.get('user')
    #     request = self.context.get("request")
    #     product_name = get_product_name_from_request(request)
    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]
    #     if value.user == request_user and value.solution in products:
    #         return value
    #     if has_project_permission("PUT", request_user, value, product_name):
    #         return value
    #     raise serializers.ValidationError(f"Project {value} does not belong to this user")
    
    def validate_company_profile(self, value):
        if value is None:
            return value
        request_user = self.context.get('user')
        if value and value == request_user.company:
            return value
        raise serializers.ValidationError(f"Current user does not belong to this {value} company")
    
    def validate_freelancer(self, value):
        if value is None:
            return value
        request_user = self.context.get('user')
        if value and value == request_user and value.is_freelancer:
            return value
        raise serializers.ValidationError(f"Only freelancer can directly associate folder with themselves")
        
    def create(self, validated_data):
        related_fields = ["project", "company_profile", "freelancer"]
        provided_fields = [field for field in related_fields if validated_data.get(field)]
        if len(provided_fields) > 1:
            raise serializers.ValidationError("Folder can only be linked to one of project, company profile, or freelancer.")
        return super().create(validated_data)
