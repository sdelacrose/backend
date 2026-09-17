from django.contrib import admin

from .models import (
    AASFieldProvenance,
    AASOutboxEvent,
    AASPublication,
    AASResourceIdentity,
    AASSubmodelFieldMapping,
    AASSubmodelTemplate,
    AASSubmodelTemplateSupplementaryFile,
    ExternalAASClient,
    ExternalAASGrant,
    FestoHandover,
    ProductAASSubmodelAssignment,
)


class AASSubmodelFieldMappingInline(admin.TabularInline):
    model = AASSubmodelFieldMapping
    extra = 0
    fields = (
        "element_path",
        "model_type",
        "source",
        "source_key",
        "editable",
        "required",
    )


class AASSubmodelTemplateSupplementaryFileInline(admin.TabularInline):
    model = AASSubmodelTemplateSupplementaryFile
    extra = 0
    fields = ("path", "content_type", "size", "sha256", "file")
    readonly_fields = fields


@admin.register(AASSubmodelTemplate)
class AASSubmodelTemplateAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "version",
        "company",
        "status",
        "id_short",
        "updated_at",
    )
    list_filter = ("status", "source_format")
    search_fields = (
        "name",
        "id_short",
        "template_identifier",
        "semantic_id",
        "company__company_name",
    )
    readonly_fields = (
        "id",
        "checksum",
        "validation_report",
        "created_at",
        "updated_at",
    )
    inlines = [
        AASSubmodelFieldMappingInline,
        AASSubmodelTemplateSupplementaryFileInline,
    ]


@admin.register(ProductAASSubmodelAssignment)
class ProductAASSubmodelAssignmentAdmin(admin.ModelAdmin):
    list_display = ("profile", "template", "assigned_by", "updated_at")
    search_fields = (
        "profile__product__name",
        "profile__product__part_number",
        "template__name",
        "template_identifier",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(AASResourceIdentity)
class AASResourceIdentityAdmin(admin.ModelAdmin):
    list_display = ("logical_aas_id", "source_type", "company", "out_of_date", "active", "updated_at")
    list_filter = ("source_type", "out_of_date", "active")
    search_fields = ("logical_aas_id", "company__company_name")
    readonly_fields = ("id", "logical_aas_id", "created_at", "updated_at")


@admin.register(AASPublication)
class AASPublicationAdmin(admin.ModelAdmin):
    list_display = ("revision", "resource", "status", "published_by", "published_at")
    list_filter = ("status",)
    search_fields = ("revision", "source_hash", "resource__logical_aas_id")
    readonly_fields = ("id", "revision", "source_hash", "created_at", "updated_at")


@admin.register(AASOutboxEvent)
class AASOutboxEventAdmin(admin.ModelAdmin):
    list_display = ("id", "resource", "event_type", "status", "attempts", "scheduled_at")
    list_filter = ("status", "event_type")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(AASFieldProvenance)
class AASFieldProvenanceAdmin(admin.ModelAdmin):
    list_display = ("resource", "field_path", "owner", "supplier", "refresh_state", "manual_override")
    list_filter = ("owner", "refresh_state", "manual_override")
    search_fields = ("resource__logical_aas_id", "field_path", "supplier")


@admin.register(ExternalAASClient)
class ExternalAASClientAdmin(admin.ModelAdmin):
    list_display = ("name", "auth0_subject", "active", "updated_at")
    list_filter = ("active",)
    search_fields = ("name", "auth0_subject")


@admin.register(ExternalAASGrant)
class ExternalAASGrantAdmin(admin.ModelAdmin):
    list_display = ("client", "resource", "publication", "can_read", "expires_at", "revoked_at")
    list_filter = ("can_read",)
    readonly_fields = ("id", "created_at")


@admin.register(FestoHandover)
class FestoHandoverAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "project",
        "status",
        "publication",
        "grant_expires_at",
        "updated_at",
    )
    list_filter = ("status",)
    search_fields = ("project__project_name", "idempotency_key")
    readonly_fields = ("id", "idempotency_key", "confirmation_digest", "created_at", "updated_at")
