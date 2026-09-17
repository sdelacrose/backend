import uuid
from pathlib import PurePosixPath

from django.conf import settings
from django.db import models
from django.db.models import Q

from models.models import CompanyProfile
from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanySolutionSpaceItem,
    Project,
    ProjectSolutionSpaceItem,
)


def aas_submodel_template_file_path(instance, filename):
    safe_name = PurePosixPath(filename).name or "supplementary-file"
    return (
        f"aas/submodel-templates/{instance.template.company_id}/"
        f"{instance.template_id}/{instance.id}/{safe_name}"
    )


class AASResourceIdentity(models.Model):
    class SourceType(models.TextChoices):
        PROJECT = "project", "Project"
        COMPANY_PRODUCT = "company_product", "Company product"
        COMPANY_ASSEMBLY = "company_assembly", "Company assembly"
        PROJECT_ASSEMBLY = "project_assembly", "Project assembly"
        FESTO_HANDOVER = "festo_handover", "Festo handover"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(CompanyProfile, related_name="aas_resource_identities", on_delete=models.CASCADE)
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    project = models.OneToOneField(Project, related_name="aas_identity", null=True, blank=True, on_delete=models.CASCADE)
    company_product = models.OneToOneField(
        AvailableProductPart,
        related_name="aas_identity",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    company_assembly = models.OneToOneField(
        CompanySolutionSpaceItem,
        related_name="aas_identity",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    project_assembly = models.OneToOneField(
        ProjectSolutionSpaceItem,
        related_name="aas_identity",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    festo_handover = models.OneToOneField(
        "FestoHandover",
        related_name="aas_identity",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    logical_aas_id = models.URLField(max_length=500, unique=True, editable=False)
    active = models.BooleanField(default=True)
    out_of_date = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        source_type="project",
                        project__isnull=False,
                        company_product__isnull=True,
                        company_assembly__isnull=True,
                        project_assembly__isnull=True,
                        festo_handover__isnull=True,
                    )
                    | Q(
                        source_type="company_product",
                        project__isnull=True,
                        company_product__isnull=False,
                        company_assembly__isnull=True,
                        project_assembly__isnull=True,
                        festo_handover__isnull=True,
                    )
                    | Q(
                        source_type="company_assembly",
                        project__isnull=True,
                        company_product__isnull=True,
                        company_assembly__isnull=False,
                        project_assembly__isnull=True,
                        festo_handover__isnull=True,
                    )
                    | Q(
                        source_type="project_assembly",
                        project__isnull=True,
                        company_product__isnull=True,
                        company_assembly__isnull=True,
                        project_assembly__isnull=False,
                        festo_handover__isnull=True,
                    )
                    | Q(
                        source_type="festo_handover",
                        project__isnull=True,
                        company_product__isnull=True,
                        company_assembly__isnull=True,
                        project_assembly__isnull=True,
                        festo_handover__isnull=False,
                    )
                ),
                name="aas_identity_exact_source",
            ),
        ]
        indexes = [models.Index(fields=["company", "source_type", "active"])]

    def save(self, *args, **kwargs):
        if not self.logical_aas_id:
            self.logical_aas_id = f"https://aas.sharemundolinks.com/ids/{self.id}"
        super().save(*args, **kwargs)


class ProductAASProfile(models.Model):
    class Origin(models.TextChoices):
        MANUFACTURED = "manufactured", "Manufactured"
        PURCHASED = "purchased", "Purchased"
        UNCLASSIFIED = "unclassified", "Unclassified"

    product = models.OneToOneField(
        AvailableProductPart,
        related_name="aas_profile",
        on_delete=models.CASCADE,
    )
    origin = models.CharField(
        max_length=24,
        choices=Origin.choices,
        default=Origin.UNCLASSIFIED,
        db_index=True,
    )
    manufacturer_name = models.CharField(max_length=255, blank=True)
    supplier_name = models.CharField(max_length=255, blank=True)
    supplier_aas_id = models.CharField(max_length=1000, blank=True)
    global_asset_id = models.CharField(max_length=1000, blank=True)
    order_code = models.CharField(max_length=255, blank=True)
    profile_key = models.CharField(max_length=100, default="idta-product")
    profile_version = models.CharField(max_length=32, default="1")
    standard_fields = models.JSONField(default=dict, blank=True)
    source_payload_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["origin", "updated_at"])]


class AASSubmodelTemplate(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        DEPRECATED = "deprecated", "Deprecated"
        INVALID = "invalid", "Invalid"

    class SourceFormat(models.TextChoices):
        JSON = "json", "JSON"
        AASX = "aasx", "AASX"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        CompanyProfile,
        related_name="aas_submodel_templates",
        on_delete=models.CASCADE,
    )
    template_identifier = models.CharField(max_length=1000)
    semantic_id = models.CharField(max_length=1000, blank=True)
    id_short = models.CharField(max_length=128)
    name = models.CharField(max_length=255)
    version = models.CharField(max_length=64)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    source_format = models.CharField(
        max_length=16,
        choices=SourceFormat.choices,
        default=SourceFormat.JSON,
    )
    definition = models.JSONField(default=dict)
    concept_descriptions = models.JSONField(default=list, blank=True)
    profile_keys = models.JSONField(default=list, blank=True)
    validation_report = models.JSONField(default=dict, blank=True)
    checksum = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_aas_submodel_templates",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="activated_aas_submodel_templates",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "template_identifier", "version"],
                name="aas_submodel_template_company_identifier_version_unique",
            ),
            models.UniqueConstraint(
                fields=["company", "template_identifier"],
                condition=Q(status="active"),
                name="aas_submodel_template_one_active_version",
            ),
        ]
        indexes = [
            models.Index(
                fields=["company", "status", "id_short"],
                name="aas_sm_tpl_company_status_idx",
            ),
            models.Index(
                fields=["company", "template_identifier"],
                name="aas_sm_tpl_company_ident_idx",
            ),
        ]
        ordering = ["name", "template_identifier", "-created_at"]


class AASSubmodelTemplateSupplementaryFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        AASSubmodelTemplate,
        related_name="supplementary_files",
        on_delete=models.CASCADE,
    )
    path = models.CharField(max_length=1000)
    content_type = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    file = models.FileField(
        upload_to=aas_submodel_template_file_path,
        max_length=1000,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["template", "path"],
                name="aas_submodel_template_file_path_unique",
            ),
        ]
        ordering = ["path"]


class ProductAASSubmodelAssignment(models.Model):
    profile = models.ForeignKey(
        ProductAASProfile,
        related_name="submodel_assignments",
        on_delete=models.CASCADE,
    )
    template = models.ForeignKey(
        AASSubmodelTemplate,
        related_name="product_assignments",
        on_delete=models.PROTECT,
    )
    template_identifier = models.CharField(max_length=1000)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="assigned_aas_product_submodels",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "template_identifier"],
                name="aas_product_submodel_assignment_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["template", "profile"],
                name="aas_product_sm_assignment_idx",
            ),
        ]
        ordering = ["template__name", "template_identifier"]


class AASSubmodelFieldMapping(models.Model):
    class Source(models.TextChoices):
        PROFILE_FIELD = "profile_field", "Product AAS field"
        PRODUCT_NAME = "product_name", "Product name"
        ARTICLE_NUMBER = "article_number", "Article number"
        MANUFACTURER_NAME = "manufacturer_name", "Manufacturer name"
        CUSTOM_FIELD = "custom_field", "Configured product column"
        TEMPLATE_DEFAULT = "template_default", "Template default"

    template = models.ForeignKey(
        AASSubmodelTemplate,
        related_name="field_mappings",
        on_delete=models.CASCADE,
    )
    element_path = models.CharField(max_length=1000)
    element_id_short = models.CharField(max_length=128)
    model_type = models.CharField(max_length=64)
    semantic_id = models.CharField(max_length=1000, blank=True)
    value_type = models.CharField(max_length=64, blank=True)
    source = models.CharField(
        max_length=32,
        choices=Source.choices,
        default=Source.PROFILE_FIELD,
    )
    source_key = models.CharField(max_length=500, blank=True)
    editable = models.BooleanField(default=True)
    required = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["template", "element_path"],
                name="aas_submodel_mapping_template_path_unique",
            ),
        ]
        ordering = ["sort_order", "element_path"]


class AASPublication(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PUBLISHING = "publishing", "Publishing"
        CURRENT = "current", "Current"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(AASResourceIdentity, related_name="publications", on_delete=models.CASCADE)
    revision = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    endpoint = models.URLField(max_length=1000, blank=True)
    json_artifact_key = models.CharField(max_length=1000, blank=True)
    aasx_artifact_key = models.CharField(max_length=1000, blank=True)
    published_identifiers = models.JSONField(default=list, blank=True)
    validation_report = models.JSONField(default=dict, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="aas_publications",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["resource", "source_hash"], name="aas_publication_resource_hash_unique"),
        ]
        indexes = [
            models.Index(fields=["resource", "-created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]


class AASResourceBinding(models.Model):
    resource = models.ForeignKey(AASResourceIdentity, related_name="bindings", on_delete=models.CASCADE)
    company_product = models.ForeignKey(AvailableProductPart, null=True, blank=True, on_delete=models.CASCADE)
    bom_part = models.ForeignKey(BillOfMaterialPart, null=True, blank=True, on_delete=models.CASCADE)
    source_field = models.CharField(max_length=255)
    submodel_id = models.CharField(max_length=500)
    element_path = models.CharField(max_length=1000)
    semantic_id = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(company_product__isnull=False, bom_part__isnull=True) | Q(company_product__isnull=True, bom_part__isnull=False)),
                name="aas_binding_exact_source",
            ),
            models.UniqueConstraint(
                fields=[
                    "resource",
                    "company_product",
                    "source_field",
                    "submodel_id",
                    "element_path",
                ],
                condition=Q(company_product__isnull=False),
                name="aas_binding_product_field_unique",
            ),
            models.UniqueConstraint(
                fields=[
                    "resource",
                    "bom_part",
                    "source_field",
                    "submodel_id",
                    "element_path",
                ],
                condition=Q(bom_part__isnull=False),
                name="aas_binding_bom_field_unique",
            ),
        ]


class AASFieldProvenance(models.Model):
    class Owner(models.TextChoices):
        LINKS = "links", "Links"
        SUPPLIER = "supplier", "Supplier"
        MANUAL = "manual", "Manual"

    resource = models.ForeignKey(AASResourceIdentity, related_name="field_provenance", on_delete=models.CASCADE)
    company_product = models.ForeignKey(AvailableProductPart, null=True, blank=True, on_delete=models.CASCADE)
    bom_part = models.ForeignKey(BillOfMaterialPart, null=True, blank=True, on_delete=models.CASCADE)
    field_path = models.CharField(max_length=1000)
    owner = models.CharField(max_length=24, choices=Owner.choices)
    supplier = models.CharField(max_length=255, blank=True)
    source_aas_id = models.CharField(max_length=1000, blank=True)
    last_value = models.JSONField(null=True, blank=True)
    last_value_hash = models.CharField(max_length=64, blank=True)
    manual_override = models.BooleanField(default=False)
    refresh_state = models.CharField(max_length=32, default="current")
    proposed_value = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(company_product__isnull=False, bom_part__isnull=True) | Q(company_product__isnull=True, bom_part__isnull=False)),
                name="aas_provenance_exact_source",
            ),
            models.UniqueConstraint(
                fields=["resource", "company_product", "field_path"],
                condition=Q(company_product__isnull=False),
                name="aas_provenance_product_field_unique",
            ),
            models.UniqueConstraint(
                fields=["resource", "bom_part", "field_path"],
                condition=Q(bom_part__isnull=False),
                name="aas_provenance_bom_field_unique",
            ),
        ]


class AASOutboxEvent(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(AASResourceIdentity, related_name="outbox_events", on_delete=models.CASCADE)
    event_type = models.CharField(max_length=64)
    payload_version = models.PositiveSmallIntegerField(default=1)
    payload = models.JSONField(default=dict)
    attempts = models.PositiveSmallIntegerField(default=0)
    scheduled_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING, db_index=True)
    failure_code = models.CharField(max_length=100, blank=True)
    failure_message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status", "scheduled_at"])]


class ExternalAASClient(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    auth0_subject = models.CharField(max_length=500, unique=True)
    oauth_client_id = models.CharField(max_length=500, blank=True, null=True, unique=True)
    certificate_fingerprint = models.CharField(max_length=128, blank=True, unique=True, null=True)
    active = models.BooleanField(default=True)
    allowed_operations = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class ExternalAASGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(ExternalAASClient, related_name="grants", on_delete=models.CASCADE)
    resource = models.ForeignKey(AASResourceIdentity, related_name="external_grants", on_delete=models.CASCADE)
    publication = models.ForeignKey(AASPublication, related_name="external_grants", on_delete=models.CASCADE)
    can_read = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["client", "publication"], name="aas_client_publication_grant_unique"),
        ]


class FestoHandover(models.Model):
    class Status(models.TextChoices):
        PREVIEWED = "previewed", "Previewed"
        PUBLISHING = "publishing", "Publishing"
        READY = "ready", "Ready"
        LAUNCHED = "launched", "Launched"
        EXPIRED = "expired", "Expired"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, related_name="festo_handovers", on_delete=models.CASCADE)
    publication = models.ForeignKey(AASPublication, related_name="festo_handovers", null=True, blank=True, on_delete=models.PROTECT)
    item_snapshot = models.JSONField(default=list)
    context_snapshot = models.JSONField(default=list)
    confirmation_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PREVIEWED)
    idempotency_key = models.CharField(max_length=128, unique=True)
    external_project_reference = models.CharField(max_length=500, blank=True)
    external_url = models.URLField(max_length=1000, blank=True)
    aas_endpoint = models.URLField(max_length=1000, blank=True)
    grant_expires_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    failure_code = models.CharField(max_length=100, blank=True)
    failure_message = models.CharField(max_length=500, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="festo_handovers", on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    launched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["project", "-created_at"]), models.Index(fields=["status", "updated_at"])]
