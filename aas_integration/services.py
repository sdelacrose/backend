import hashlib
import json
import re
from dataclasses import asdict
from datetime import timedelta, timezone as datetime_timezone
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanySolutionSpaceItem,
    Project,
    ProjectSolutionSpaceItem,
)

from .adapters import BasyxAASClient, DjangoArtifactStore, base64url, create_aasx, parse_aas_environment
from .domain import (
    FestoHandoverAASMapper,
    ProductAASMapper,
    ProjectBOMAASMapper,
    SolutionSpaceAASMapper,
    canonical_json,
    source_hash,
    validation_report,
)
from .models import (
    AASFieldProvenance,
    AASOutboxEvent,
    AASPublication,
    AASResourceBinding,
    AASResourceIdentity,
    ExternalAASClient,
    ExternalAASGrant,
    FestoHandover,
    ProductAASProfile,
)


CONFIRMATION_SALT = "aas_integration.festo_handover"


def _normalized_company_name(value: Any) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").casefold())


def infer_aas_origin_from_values(
    *,
    company_name: Any,
    manufacturer: Any = "",
    supplier: Any = "",
    supplier_aas_id: Any = "",
) -> str:
    manufacturer = str(manufacturer or "").strip()
    supplier = str(supplier or "").strip()
    supplier_aas_id = str(supplier_aas_id or "").strip()
    company_name = str(company_name or "").strip()
    normalized_company = _normalized_company_name(company_name)
    normalized_manufacturer = _normalized_company_name(manufacturer)
    normalized_supplier = _normalized_company_name(supplier)

    if normalized_company and normalized_manufacturer == normalized_company:
        return ProductAASProfile.Origin.MANUFACTURED
    if supplier_aas_id:
        return ProductAASProfile.Origin.PURCHASED
    if manufacturer:
        return ProductAASProfile.Origin.PURCHASED
    if supplier:
        if normalized_company and normalized_supplier == normalized_company:
            return ProductAASProfile.Origin.MANUFACTURED
        return ProductAASProfile.Origin.PURCHASED
    return ProductAASProfile.Origin.UNCLASSIFIED


def infer_product_aas_origin(
    product: AvailableProductPart,
    profile: ProductAASProfile | None = None,
) -> str:
    fields = product.custom_fields or {}
    return infer_aas_origin_from_values(
        company_name=getattr(product.company_profile, "company_name", ""),
        manufacturer=(
            getattr(profile, "manufacturer_name", "")
            or fields.get("manufacturer")
        ),
        supplier=getattr(profile, "supplier_name", "") or fields.get("supplier"),
        supplier_aas_id=(
            getattr(profile, "supplier_aas_id", "")
            or fields.get("supplier_aas_id")
            or fields.get("aas_id")
        ),
    )


def synchronize_product_aas_profile(
    product: AvailableProductPart,
) -> ProductAASProfile:
    profile, _ = ProductAASProfile.objects.get_or_create(product=product)
    fields = product.custom_fields or {}
    candidate_values = {
        "manufacturer_name": fields.get("manufacturer"),
        "supplier_name": fields.get("supplier"),
        "supplier_aas_id": fields.get("supplier_aas_id") or fields.get("aas_id"),
        "global_asset_id": fields.get("global_asset_id") or fields.get("asset_id"),
        "order_code": (
            fields.get("order_code")
            or fields.get("article_number")
            or product.part_number
        ),
    }
    update_fields = []
    for field, value in candidate_values.items():
        normalized = str(value or "").strip()
        if normalized and not getattr(profile, field):
            setattr(profile, field, normalized)
            update_fields.append(field)
    if profile.origin == ProductAASProfile.Origin.UNCLASSIFIED:
        inferred_origin = infer_product_aas_origin(product, profile)
        if inferred_origin != ProductAASProfile.Origin.UNCLASSIFIED:
            profile.origin = inferred_origin
            update_fields.append("origin")
    if update_fields:
        profile.save(update_fields=[*update_fields, "updated_at"])
    return profile


def get_or_create_project_identity(project: Project) -> AASResourceIdentity:
    company = project.company_profile or getattr(project.user, "company", None)
    if not company:
        raise ValidationError("The project has no company owner for AAS publication.")
    identity, _ = AASResourceIdentity.objects.get_or_create(
        project=project,
        defaults={"company": company, "source_type": AASResourceIdentity.SourceType.PROJECT},
    )
    return identity


def get_or_create_product_identity(product: AvailableProductPart) -> AASResourceIdentity:
    identity, _ = AASResourceIdentity.objects.get_or_create(
        company_product=product,
        defaults={"company": product.company_profile, "source_type": AASResourceIdentity.SourceType.COMPANY_PRODUCT},
    )
    return identity


def get_or_create_company_assembly_identity(
    assembly: CompanySolutionSpaceItem,
) -> AASResourceIdentity:
    identity, _ = AASResourceIdentity.objects.get_or_create(
        company_assembly=assembly,
        defaults={
            "company": assembly.company_profile,
            "source_type": AASResourceIdentity.SourceType.COMPANY_ASSEMBLY,
        },
    )
    return identity


def get_or_create_project_assembly_identity(
    assembly: ProjectSolutionSpaceItem,
) -> AASResourceIdentity:
    company = assembly.project.company_profile or getattr(
        assembly.project.user,
        "company",
        None,
    )
    if not company:
        raise ValidationError("The project has no company owner for AAS publication.")
    identity, _ = AASResourceIdentity.objects.get_or_create(
        project_assembly=assembly,
        defaults={
            "company": company,
            "source_type": AASResourceIdentity.SourceType.PROJECT_ASSEMBLY,
        },
    )
    return identity


def get_or_create_festo_handover_identity(handover: FestoHandover) -> AASResourceIdentity:
    company = handover.project.company_profile or getattr(handover.project.user, "company", None)
    if not company:
        raise ValidationError("The project has no company owner for AAS publication.")
    identity, _ = AASResourceIdentity.objects.get_or_create(
        festo_handover=handover,
        defaults={
            "company": company,
            "source_type": AASResourceIdentity.SourceType.FESTO_HANDOVER,
        },
    )
    return identity


def map_resource(identity: AASResourceIdentity):
    if identity.source_type == AASResourceIdentity.SourceType.PROJECT:
        return ProjectBOMAASMapper().map(identity.project, identity.logical_aas_id)
    if identity.source_type == AASResourceIdentity.SourceType.FESTO_HANDOVER:
        return FestoHandoverAASMapper().map(identity.festo_handover, identity.logical_aas_id)
    if identity.source_type == AASResourceIdentity.SourceType.COMPANY_ASSEMBLY:
        return SolutionSpaceAASMapper().map(
            identity.company_assembly,
            identity.logical_aas_id,
        )
    if identity.source_type == AASResourceIdentity.SourceType.PROJECT_ASSEMBLY:
        return SolutionSpaceAASMapper().map(
            identity.project_assembly,
            identity.logical_aas_id,
        )
    synchronize_product_aas_profile(identity.company_product)
    return ProductAASMapper().map(identity.company_product, identity.logical_aas_id)


def validate_project(project: Project) -> dict[str, Any]:
    identity = getattr(project, "aas_identity", None)
    aas_id = identity.logical_aas_id if identity else "https://aas.sharemundolinks.com/ids/pending-project"
    return validation_report(ProjectBOMAASMapper().map(project, aas_id))


def validate_product(product: AvailableProductPart) -> dict[str, Any]:
    synchronize_product_aas_profile(product)
    identity = getattr(product, "aas_identity", None)
    aas_id = identity.logical_aas_id if identity else "https://aas.sharemundolinks.com/ids/pending-product"
    return validation_report(ProductAASMapper().map(product, aas_id))


def validate_company_assembly(assembly: CompanySolutionSpaceItem) -> dict[str, Any]:
    identity = getattr(assembly, "aas_identity", None)
    aas_id = (
        identity.logical_aas_id
        if identity
        else "https://aas.sharemundolinks.com/ids/pending-company-assembly"
    )
    return validation_report(SolutionSpaceAASMapper().map(assembly, aas_id))


def validate_project_assembly(assembly: ProjectSolutionSpaceItem) -> dict[str, Any]:
    identity = getattr(assembly, "aas_identity", None)
    aas_id = (
        identity.logical_aas_id
        if identity
        else "https://aas.sharemundolinks.com/ids/pending-project-assembly"
    )
    return validation_report(SolutionSpaceAASMapper().map(assembly, aas_id))


def enqueue_publication(identity: AASResourceIdentity, *, actor_id: int | None, immediate: bool = False) -> AASOutboxEvent:
    scheduled_at = timezone.now() if immediate else timezone.now() + timedelta(seconds=settings.AAS_PUBLICATION_DEBOUNCE_SECONDS)
    identity.out_of_date = True
    identity.save(update_fields=["out_of_date", "updated_at"])
    pending = AASOutboxEvent.objects.filter(
        resource=identity,
        event_type="publish",
        status=AASOutboxEvent.Status.PENDING,
    ).order_by("-created_at").first()
    if pending:
        pending.scheduled_at = min(pending.scheduled_at, scheduled_at) if immediate else scheduled_at
        pending.payload = {"actor_id": actor_id}
        pending.save(update_fields=["scheduled_at", "payload", "updated_at"])
        return pending
    return AASOutboxEvent.objects.create(
        resource=identity,
        event_type="publish",
        payload={"actor_id": actor_id},
        scheduled_at=scheduled_at,
    )


def _wake_publication_worker(event: AASOutboxEvent) -> None:
    if settings.AAS_AUTO_PUBLISH_ENABLED:
        from .tasks import process_aas_outbox_event

        transaction.on_commit(lambda: process_aas_outbox_event.apply_async(args=[str(event.id)], eta=event.scheduled_at))


def mark_project_out_of_date(project: Project, actor_id: int | None) -> None:
    identity = getattr(project, "aas_identity", None)
    if not BillOfMaterialPart.objects.filter(project=project).exists():
        if identity:
            identity.out_of_date = True
            identity.save(update_fields=["out_of_date", "updated_at"])
        return
    if not identity and not settings.AAS_AUTO_PUBLISH_ENABLED:
        return
    identity = identity or get_or_create_project_identity(project)
    identity.out_of_date = True
    identity.save(update_fields=["out_of_date", "updated_at"])
    if settings.AAS_AUTO_PUBLISH_ENABLED:
        _wake_publication_worker(enqueue_publication(identity, actor_id=actor_id))


def mark_product_out_of_date(product: AvailableProductPart, actor_id: int | None) -> None:
    identity = getattr(product, "aas_identity", None)
    if not identity and not settings.AAS_AUTO_PUBLISH_ENABLED:
        return
    identity = identity or get_or_create_product_identity(product)
    identity.out_of_date = True
    identity.save(update_fields=["out_of_date", "updated_at"])
    if settings.AAS_AUTO_PUBLISH_ENABLED:
        _wake_publication_worker(enqueue_publication(identity, actor_id=actor_id))


def _mark_assembly_chain_out_of_date(assembly, actor_id: int | None) -> None:
    current = assembly
    while current is not None:
        identity = getattr(current, "aas_identity", None)
        if identity:
            identity.out_of_date = True
            identity.save(update_fields=["out_of_date", "updated_at"])
            if settings.AAS_AUTO_PUBLISH_ENABLED:
                _wake_publication_worker(
                    enqueue_publication(identity, actor_id=actor_id)
                )
        current = current.parent


def mark_company_assembly_out_of_date(
    assembly: CompanySolutionSpaceItem,
    actor_id: int | None,
) -> None:
    _mark_assembly_chain_out_of_date(assembly, actor_id)


def mark_project_assembly_out_of_date(
    assembly: ProjectSolutionSpaceItem,
    actor_id: int | None,
) -> None:
    _mark_assembly_chain_out_of_date(assembly, actor_id)


def _replace_bindings(identity: AASResourceIdentity, document) -> None:
    AASResourceBinding.objects.filter(resource=identity).delete()
    bindings = []
    for row in document.bindings:
        bindings.append(AASResourceBinding(resource=identity, **row))
    AASResourceBinding.objects.bulk_create(bindings)


def _upsert_provenance(identity: AASResourceIdentity, document) -> None:
    for row in document.provenance:
        lookup = {
            "resource": identity,
            "company_product_id": row.get("company_product_id"),
            "bom_part_id": row.get("bom_part_id"),
            "field_path": row["field_path"],
        }
        existing = AASFieldProvenance.objects.filter(**lookup).first()
        incoming_hash = source_hash(row.get("last_value"))
        refresh_state = existing.refresh_state if existing and existing.refresh_state == "review_required" else "current"
        proposed_value = existing.proposed_value if refresh_state == "review_required" else None
        AASFieldProvenance.objects.update_or_create(
            **lookup,
            defaults={
                "owner": row["owner"],
                "supplier": row.get("supplier", ""),
                "source_aas_id": row.get("source_aas_id", ""),
                "last_value": row.get("last_value"),
                "last_value_hash": incoming_hash,
                "manual_override": row.get("manual_override", False),
                "refresh_state": refresh_state,
                "proposed_value": proposed_value,
            },
        )


@transaction.atomic
def apply_supplier_product_fields(
    product: AvailableProductPart,
    fields: dict[str, Any],
    *,
    supplier: str,
    source_aas_id: str,
    actor_id: int | None = None,
) -> dict[str, list[str]]:
    identity = get_or_create_product_identity(product)
    custom_fields = dict(product.custom_fields or {})
    updated: list[str] = []
    proposed: list[str] = []
    for key, value in fields.items():
        if not key or key.endswith("_manual_override"):
            continue
        field_path = f"custom_fields.{key}"
        provenance = AASFieldProvenance.objects.filter(
            resource=identity,
            company_product=product,
            field_path=field_path,
        ).first()
        is_manual = bool(custom_fields.get(f"{key}_manual_override")) or bool(
            provenance and provenance.manual_override
        )
        if is_manual and custom_fields.get(key) != value:
            if provenance:
                provenance.refresh_state = "review_required"
                provenance.proposed_value = value
                provenance.supplier = supplier
                provenance.source_aas_id = source_aas_id
                provenance.save(
                    update_fields=[
                        "refresh_state",
                        "proposed_value",
                        "supplier",
                        "source_aas_id",
                        "updated_at",
                    ]
                )
            else:
                AASFieldProvenance.objects.create(
                    resource=identity,
                    company_product=product,
                    field_path=field_path,
                    owner=AASFieldProvenance.Owner.MANUAL,
                    supplier=supplier,
                    source_aas_id=source_aas_id,
                    last_value=custom_fields.get(key),
                    last_value_hash=source_hash(custom_fields.get(key)),
                    manual_override=True,
                    refresh_state="review_required",
                    proposed_value=value,
                )
            proposed.append(key)
            continue
        custom_fields[key] = value
        AASFieldProvenance.objects.update_or_create(
            resource=identity,
            company_product=product,
            bom_part=None,
            field_path=field_path,
            defaults={
                "owner": AASFieldProvenance.Owner.SUPPLIER,
                "supplier": supplier,
                "source_aas_id": source_aas_id,
                "last_value": value,
                "last_value_hash": source_hash(value),
                "manual_override": False,
                "refresh_state": "current",
                "proposed_value": None,
            },
        )
        updated.append(key)
    if updated:
        product.custom_fields = custom_fields
        product.save(update_fields=["custom_fields", "updated_at"])
        mark_product_out_of_date(product, actor_id)
    return {"updated": updated, "proposed": proposed}


def publish_resource(identity_id, actor=None, repository=None, artifact_store=None) -> AASPublication:
    repository = repository or BasyxAASClient()
    artifact_store = artifact_store or DjangoArtifactStore()
    restore_existing = False
    with transaction.atomic():
        identity = AASResourceIdentity.objects.select_for_update().get(id=identity_id)
        document = map_resource(identity)
        report = validation_report(document)
        if not report["valid"]:
            raise ValidationError(report)
        try:
            parse_aas_environment(document.as_environment())
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError({"aas_model": f"The generated AAS model is invalid: {exc}"}) from exc
        existing = AASPublication.objects.filter(
            resource=identity,
            source_hash=document.hash,
            status=AASPublication.Status.CURRENT,
        ).first()
        if existing:
            publication = existing
            restore_existing = True
        else:
            publication, created = AASPublication.objects.get_or_create(
                resource=identity,
                source_hash=document.hash,
                defaults={"published_by": actor, "validation_report": report, "status": AASPublication.Status.PUBLISHING},
            )
            if not created and publication.status == AASPublication.Status.PUBLISHING:
                raise ValidationError("This AAS resource is already being published.")
            publication.status = AASPublication.Status.PUBLISHING
            publication.validation_report = report
            publication.published_by = actor
            publication.save(update_fields=["status", "validation_report", "published_by", "updated_at"])

    revision_aas_id = f"{identity.logical_aas_id}/revisions/{publication.revision}"
    if restore_existing:
        expected_environment = document.as_revision_repository_environment(
            revision_aas_id
        )
        try:
            read_back = repository.read_environment(revision_aas_id)
            if source_hash(read_back) != source_hash(expected_environment):
                raise ValidationError("The stored AAS revision does not match the current publication.")
        except Exception:
            try:
                repository.publish(document, revision_aas_id)
                read_back = repository.read_environment(revision_aas_id)
                if source_hash(read_back) != source_hash(expected_environment):
                    raise ValidationError("The restored AAS revision did not match the current publication.")
            except Exception:
                AASResourceIdentity.objects.filter(id=identity_id).update(
                    out_of_date=True,
                    updated_at=timezone.now(),
                )
                raise
        activated_at = timezone.now()
        identifiers = _published_identifiers(read_back)
        with transaction.atomic():
            AASResourceIdentity.objects.filter(id=identity_id).update(
                out_of_date=False,
                updated_at=activated_at,
            )
            AASPublication.objects.filter(id=publication.id).update(
                published_at=activated_at,
                published_by=actor,
                published_identifiers=identifiers,
                updated_at=activated_at,
            )
        publication.refresh_from_db()
        return publication

    try:
        repository_environment = repository.publish(document, revision_aas_id)
        read_back = repository.read_environment(revision_aas_id)
        if source_hash(read_back) != source_hash(repository_environment):
            raise ValidationError("The AAS repository read-back did not match the published revision.")
        revision_environment = document.as_revision_environment(revision_aas_id)
        prefix = f"aas/{identity.company_id}/{identity.id}/{publication.revision}"
        json_key = artifact_store.put_json(f"{prefix}/environment.json", canonical_json(revision_environment))
        aasx_key = artifact_store.put_aasx(
            f"{prefix}/environment.aasx",
            create_aasx(
                revision_environment,
                supplementary_files=document.supplementary_files,
            ),
        )
    except Exception as exc:
        AASPublication.objects.filter(id=publication.id).update(
            status=AASPublication.Status.FAILED,
            validation_report={
                **report,
                "repository_error": {
                    "code": type(exc).__name__,
                    "message": "The AAS publication could not be completed.",
                },
            },
            updated_at=timezone.now(),
        )
        raise

    with transaction.atomic():
        identity = AASResourceIdentity.objects.select_for_update().get(id=identity_id)
        publication = AASPublication.objects.select_for_update().get(id=publication.id)
        publication.status = AASPublication.Status.CURRENT
        api_version = (
            "v3.1"
            if identity.source_type == AASResourceIdentity.SourceType.FESTO_HANDOVER
            else getattr(settings, "AAS_EXTERNAL_API_VERSION", "v3.1")
        )
        publication.endpoint = f"{settings.AAS_EXTERNAL_BASE_URL.rstrip('/')}/api/{api_version}/shells/{_encode_identifier(revision_aas_id)}"
        publication.json_artifact_key = json_key
        publication.aasx_artifact_key = aasx_key
        publication.published_identifiers = _published_identifiers(read_back)
        publication.published_at = timezone.now()
        publication.save(update_fields=["status", "endpoint", "json_artifact_key", "aasx_artifact_key", "published_identifiers", "published_at", "updated_at"])
        identity.out_of_date = False
        identity.save(update_fields=["out_of_date", "updated_at"])
        _replace_bindings(identity, document)
        _upsert_provenance(identity, document)
        return publication


def _encode_identifier(value: str) -> str:
    import base64

    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _published_identifiers(environment: dict) -> list[str]:
    identifiers = {
        str(item.get("id"))
        for collection in ("assetAdministrationShells", "submodels")
        for item in environment.get(collection, [])
        if isinstance(item, dict) and item.get("id")
    }
    return sorted(identifiers)


def current_publication(identity: AASResourceIdentity) -> AASPublication | None:
    return identity.publications.filter(status=AASPublication.Status.CURRENT).order_by("-published_at").first()


def resource_summary(identity: AASResourceIdentity) -> dict[str, Any]:
    publication = current_publication(identity)
    if not publication:
        status = "unpublished"
    elif identity.out_of_date:
        status = "out_of_date"
    else:
        status = "current"
    latest = identity.publications.order_by("-created_at").first()
    if latest and latest.status in {AASPublication.Status.PUBLISHING, AASPublication.Status.FAILED}:
        status = latest.status
    return {
        "resource_id": str(identity.id),
        "aas_id": identity.logical_aas_id,
        "status": status,
        "out_of_date": identity.out_of_date,
        "current_publication": publication,
        "latest_publication": latest,
    }


FESTO_ASSET_ID_PREFIX = "https://smart.festo.com/asset/type/oc/"
FESTO_ORDER_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _festo_snapshot(
    project: Project,
    selected_ids: set[int] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    eligible, excluded = [], []
    rows = list(
        BillOfMaterialPart.objects.filter(project=project)
        .select_related("parent", "available_product__aas_profile")
        .order_by("level", "sort_order", "id")
    )
    row_by_id = {row.id: row for row in rows}
    for row in rows:
        custom_fields = row.custom_fields or {}
        product_profile = getattr(row.available_product, "aas_profile", None) if row.available_product_id else None
        supplier = str(
            custom_fields.get("supplier")
            or (product_profile.supplier_name if product_profile else "")
            or ""
        ).strip()
        order_code = str(
            custom_fields.get("order_code")
            or (product_profile.order_code if product_profile else "")
            or row.part_number
            or custom_fields.get("article_number")
            or ""
        ).strip()
        record = {
            "row_id": row.id,
            "parent_row_id": row.parent_id,
            "hierarchy": row.get_sequence(),
            "name": row.name,
            "article_number": order_code,
            "quantity": custom_fields.get("quantity", 1),
        }
        if supplier.casefold() != "festo":
            excluded.append({**record, "reason": "not_festo"})
        elif not order_code:
            excluded.append({**record, "reason": "missing_order_code"})
        elif not FESTO_ORDER_CODE_PATTERN.fullmatch(order_code):
            excluded.append({**record, "reason": "invalid_order_code"})
        elif selected_ids is not None and row.id not in selected_ids:
            excluded.append({**record, "reason": "not_selected"})
        else:
            eligible.append(
                {
                    **record,
                    "asset_id": f"{FESTO_ASSET_ID_PREFIX}{order_code}",
                }
            )

    eligible_ids = {row["row_id"] for row in eligible}
    for record in eligible:
        parent_id = record["parent_row_id"]
        while parent_id and parent_id not in eligible_ids:
            parent = row_by_id.get(parent_id)
            parent_id = parent.parent_id if parent else None
        record["parent_row_id"] = parent_id
    return eligible, [], excluded


def _festo_grant_expiry(now=None):
    now = now or timezone.now()
    expiry = now + timedelta(seconds=settings.FESTO_GRANT_TTL_SECONDS)
    raw_minimum = str(settings.FESTO_GRANT_MIN_EXPIRES_AT or "").strip()
    if not raw_minimum:
        return expiry
    minimum = parse_datetime(raw_minimum)
    if minimum is None:
        raise ValidationError("FESTO_GRANT_MIN_EXPIRES_AT must be an ISO 8601 date-time.")
    if timezone.is_naive(minimum):
        minimum = timezone.make_aware(minimum, datetime_timezone.utc)
    return max(expiry, minimum)


@transaction.atomic
def create_festo_preview(
    project: Project,
    user,
    selected_ids: set[int] | None = None,
) -> tuple[FestoHandover, str, list[dict]]:
    eligible, context, excluded = _festo_snapshot(project, selected_ids)
    if not eligible:
        raise ValidationError("No eligible Festo BOM items were found.")
    digest = source_hash({"items": eligible, "context": context})
    idempotency_key = hashlib.sha256(f"{project.id}:{digest}".encode("utf-8")).hexdigest()
    handover, _ = FestoHandover.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "project": project,
            "item_snapshot": eligible,
            "context_snapshot": context,
            "confirmation_digest": digest,
            "requested_by": user,
        },
    )
    if handover.status in {
        FestoHandover.Status.FAILED,
        FestoHandover.Status.CANCELLED,
        FestoHandover.Status.EXPIRED,
    }:
        handover.status = FestoHandover.Status.PREVIEWED
        handover.failure_code = ""
        handover.failure_message = ""
        handover.save(update_fields=["status", "failure_code", "failure_message", "updated_at"])
    token = signing.dumps(
        {"handover_id": str(handover.id), "user_id": user.id, "project_id": project.id, "digest": digest},
        salt=CONFIRMATION_SALT,
        compress=True,
    )
    return handover, token, excluded


def confirm_festo_handover(handover_id, token: str, user, repository=None, artifact_store=None) -> FestoHandover:
    client_id = str(settings.FESTO_AAS_CLIENT_ID or "").strip()
    if not client_id:
        raise ValidationError("The Festo OAuth client is not configured.")
    grant_expires_at = _festo_grant_expiry()
    try:
        signed = signing.loads(token, salt=CONFIRMATION_SALT, max_age=settings.FESTO_CONFIRMATION_MAX_AGE_SECONDS)
    except signing.BadSignature as exc:
        raise PermissionDenied("The Festo handover confirmation has expired or is invalid.") from exc
    with transaction.atomic():
        handover = FestoHandover.objects.select_for_update().get(id=handover_id)
        if signed != {"handover_id": str(handover.id), "user_id": user.id, "project_id": handover.project_id, "digest": handover.confirmation_digest}:
            raise PermissionDenied("The confirmation does not match this handover.")
        selected_ids = {int(row["row_id"]) for row in handover.item_snapshot}
        current_snapshot, current_context, _ = _festo_snapshot(handover.project, selected_ids)
        if source_hash({"items": current_snapshot, "context": current_context}) != handover.confirmation_digest:
            raise ValidationError("The BOM changed after preview. Review the handover again.")
        if handover.status in {FestoHandover.Status.READY, FestoHandover.Status.LAUNCHED}:
            return handover
        if handover.status == FestoHandover.Status.PUBLISHING:
            raise ValidationError("This Festo handover is already processing.")
        handover.status = FestoHandover.Status.PUBLISHING
        handover.confirmed_at = timezone.now()
        handover.attempts += 1
        handover.failure_code = ""
        handover.failure_message = ""
        handover.save(
            update_fields=[
                "status",
                "confirmed_at",
                "attempts",
                "failure_code",
                "failure_message",
                "updated_at",
            ]
        )

    try:
        identity = get_or_create_festo_handover_identity(handover)
        publication = publish_resource(
            identity.id,
            actor=user,
            repository=repository,
            artifact_store=artifact_store,
        )
        client_subject = f"oauth-client:{client_id}"
        client = ExternalAASClient.objects.filter(oauth_client_id=client_id).first()
        if client is None:
            client = ExternalAASClient(
                auth0_subject=client_subject,
                oauth_client_id=client_id,
            )
        client.name = "Festo"
        client.auth0_subject = client_subject
        client.oauth_client_id = client_id
        client.allowed_operations = ["read"]
        client.active = True
        client.save()
        ExternalAASGrant.objects.update_or_create(
            client=client,
            publication=publication,
            defaults={
                "resource": identity,
                "can_read": True,
                "expires_at": grant_expires_at,
                "revoked_at": None,
                "created_by": user,
            },
        )
        launch_url = f"{settings.FESTO_PROJECT_LAUNCH_URL}?{urlencode({'aasEndpoint': base64url(publication.endpoint)})}"
    except Exception as exc:
        FestoHandover.objects.filter(id=handover_id).update(
            status=FestoHandover.Status.FAILED,
            failure_code=type(exc).__name__,
            failure_message="The Festo handover package could not be prepared. Any previous publication remains available.",
            updated_at=timezone.now(),
        )
        raise

    with transaction.atomic():
        handover = FestoHandover.objects.select_for_update().get(id=handover_id)
        handover.status = FestoHandover.Status.READY
        handover.publication = publication
        handover.aas_endpoint = publication.endpoint
        handover.external_url = launch_url
        handover.grant_expires_at = grant_expires_at
        handover.failure_code = ""
        handover.failure_message = ""
        handover.save(
            update_fields=[
                "status",
                "publication",
                "aas_endpoint",
                "external_url",
                "grant_expires_at",
                "failure_code",
                "failure_message",
                "updated_at",
            ]
        )
        return handover


@transaction.atomic
def mark_festo_handover_launched(handover: FestoHandover) -> FestoHandover:
    handover = FestoHandover.objects.select_for_update().get(id=handover.id)
    if handover.status == FestoHandover.Status.LAUNCHED:
        return handover
    if handover.status != FestoHandover.Status.READY:
        raise ValidationError("The Festo handover is not ready to launch.")
    if handover.grant_expires_at and handover.grant_expires_at <= timezone.now():
        handover.status = FestoHandover.Status.EXPIRED
        handover.save(update_fields=["status", "updated_at"])
        raise ValidationError("The Festo access grant has expired. Create a new preview.")
    handover.status = FestoHandover.Status.LAUNCHED
    handover.launched_at = timezone.now()
    handover.save(update_fields=["status", "launched_at", "updated_at"])
    return handover
