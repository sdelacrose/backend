import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q

from aas_integration.domain import source_hash
from aas_integration.models import ProductAASProfile
from aas_integration.services import (
    get_or_create_product_identity,
    mark_product_out_of_date,
)
from models.models import CompanyProfile
from project_management.models import AvailableProductPart, CompanySolutionSpaceItem


@dataclass(frozen=True)
class SupplierProductImportResult:
    assembly: CompanySolutionSpaceItem
    product: AvailableProductPart
    created_product: bool
    created_assembly: bool


def _next_root_order(model, company_profile):
    max_order = model.objects.filter(
        company_profile=company_profile,
        parent__isnull=True,
    ).aggregate(max_order=Max("sort_order"))["max_order"]
    return (max_order or 0) + 1


def _field_key(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_")
    if not normalized:
        normalized = "SupplierField"
    if not normalized[0].isalpha():
        normalized = f"A_{normalized}"
    return normalized[:128]


def _supplier_technical_fields(fields: list[dict]) -> list[dict]:
    normalized = []
    for field in fields:
        normalized.append(
            {
                "key": _field_key(field["key"]),
                "label": field["label"].strip(),
                "value": field["value"].strip(),
                "semantic_id": field.get("semantic_id", "").strip(),
                "value_type": "xs:string",
            }
        )
    return sorted(normalized, key=lambda field: field["key"].casefold())


def _existing_profile(company_profile, supplier_aas_id, global_asset_id):
    identity_filter = Q()
    if supplier_aas_id:
        identity_filter |= Q(supplier_aas_id=supplier_aas_id)
    if global_asset_id:
        identity_filter |= Q(global_asset_id=global_asset_id)
    return (
        ProductAASProfile.objects.select_related(
            "product",
            "product__solution_space_assembly",
        )
        .filter(product__company_profile=company_profile)
        .filter(identity_filter)
        .order_by("product_id")
        .first()
    )


@transaction.atomic
def import_supplier_product_to_company_catalog(
    *,
    company_profile,
    actor,
    payload: dict,
) -> SupplierProductImportResult:
    # Serializing imports per company keeps identity-based deduplication race-free.
    company_profile = CompanyProfile.objects.select_for_update().get(
        pk=company_profile.pk,
    )
    supplier_aas_id = payload.get("supplier_aas_id", "").strip()
    global_asset_id = payload.get("global_asset_id", "").strip()
    existing_profile = _existing_profile(
        company_profile,
        supplier_aas_id,
        global_asset_id,
    )
    if existing_profile:
        if existing_profile.origin != ProductAASProfile.Origin.PURCHASED:
            raise ValidationError(
                "The supplier AAS identity is already assigned to a non-purchased product."
            )
        product = existing_profile.product
        assembly = getattr(product, "solution_space_assembly", None)
        created_assembly = assembly is None
        if assembly is None:
            assembly = CompanySolutionSpaceItem.objects.create(
                company_profile=company_profile,
                aas_product=product,
                sort_order=_next_root_order(
                    CompanySolutionSpaceItem,
                    company_profile,
                ),
                assembly_name=product.name,
                assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
                custom_fields={
                    "source": payload["source"],
                    "supplier": existing_profile.supplier_name,
                    "supplier_aas_id": existing_profile.supplier_aas_id,
                    "global_asset_id": existing_profile.global_asset_id,
                },
                created_by=actor,
                modified_by=actor,
            )
        return SupplierProductImportResult(
            assembly=assembly,
            product=product,
            created_product=False,
            created_assembly=created_assembly,
        )

    technical_fields = _supplier_technical_fields(payload["technical_fields"])
    manufacturer = next(
        (
            field["value"]
            for field in technical_fields
            if field["key"].casefold() == "manufacturer"
        ),
        payload["supplier"],
    )
    article_number = payload.get("article_number", "").strip()
    custom_fields = {
        "supplier": payload["supplier"],
        "manufacturer": manufacturer,
        "supplier_aas_id": supplier_aas_id,
        "aas_id": supplier_aas_id,
        "global_asset_id": global_asset_id,
        "asset_id": global_asset_id,
        "order_code": article_number,
    }
    product = AvailableProductPart.objects.create(
        company_profile=company_profile,
        sort_order=_next_root_order(AvailableProductPart, company_profile),
        name=payload["name"],
        part_number=article_number or None,
        custom_fields=custom_fields,
        created_by=actor,
        modified_by=actor,
    )
    import_snapshot = {
        "source": payload["source"],
        "source_product_id": payload.get("source_product_id", ""),
        "asset_kind": payload.get("asset_kind", "Type"),
        "product_url": payload.get("product_url", ""),
        "image_url": payload.get("image_url", ""),
        "technical_fields": technical_fields,
    }
    ProductAASProfile.objects.create(
        product=product,
        origin=ProductAASProfile.Origin.PURCHASED,
        manufacturer_name=manufacturer,
        supplier_name=payload["supplier"],
        supplier_aas_id=supplier_aas_id,
        global_asset_id=global_asset_id,
        order_code=article_number,
        standard_fields={"supplier_import": import_snapshot},
        source_payload_hash=source_hash(
            {
                "name": payload["name"],
                "article_number": article_number,
                "supplier": payload["supplier"],
                "supplier_aas_id": supplier_aas_id,
                "global_asset_id": global_asset_id,
                **import_snapshot,
            }
        ),
    )
    assembly = CompanySolutionSpaceItem.objects.create(
        company_profile=company_profile,
        aas_product=product,
        sort_order=_next_root_order(CompanySolutionSpaceItem, company_profile),
        assembly_name=payload["name"],
        assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        custom_fields={
            "source": payload["source"],
            "supplier": payload["supplier"],
            "supplier_aas_id": supplier_aas_id,
            "global_asset_id": global_asset_id,
        },
        created_by=actor,
        modified_by=actor,
    )
    identity = get_or_create_product_identity(product)
    product.aas_identity = identity
    mark_product_out_of_date(product, actor.id)
    return SupplierProductImportResult(
        assembly=assembly,
        product=product,
        created_product=True,
        created_assembly=True,
    )
