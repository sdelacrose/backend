from django.db import migrations

NEW_PROJECT_DOCUMENT_TYPES = [
    "Architectural view",
    "Area classification drawing",
    "Cable schedule, pipe support and spring schedule",
    "Calculations, curves, tables",
    "Call for meeting, MoM, meeting presentation, etc.",
    "Cause and effect chart",
    "Contract",
    "Contracting strategy",
    "Correspondence",
    "Correspondence of contract",
    "D model",
    "Data sheet, according to discipline",
    "Detail drawing",
    "Diagram; engineering, functional, block, interconnection, nodal, circuit",
    "Drawing",
    "Equipment layout",
    "Estimate, budget",
    "Fire protection drawing",
    "Frame agreement",
    "General and detailed specifications",
    "General arrangement drawing (guide drawing)",
    "Heat and mass balance",
    "Heat tracing drawing",
    "Internal Due Diligence (IDD)",
    "Isometric drawing",
    "ITB related (bid clarification)",
    "Laboratory results & reports",
    "Layout of any kind (plot plan, site plan etc.)",
    "Lists of any kind, index, legend",
    "Loading diagram, loading plan, structural loads",
    "Logic and sequence diagram",
    "Logistic",
    "Loop diagram",
    "Maintenance information (e.g. Maintenance manual)",
    "Material receiving report",
    "Material take-off (MTO)",
    "Mechanical Completion and Commissioning documents",
    "Miscellaneous (not covered by any specific document type above)",
    "Offer/quotation",
    "Operational information (e.g. Operating manual)",
    "Org. chart, functional descriptions etc.",
    "Permitting and Licensing",
    "Piping and instrumentation diagram (PID)",
    "Process flow diagram, utility flow diagram",
    "Project control (MPR, Cost report, close-out report, etc.)",
    "Project design criteria; design basis, principal decisions, PCP",
    "Project procedure, work instruction, manual",
    "Purchase order",
    "Report of any kind, according to discipline",
    "Requisition",
    "Sales agreement (EAR or Project Agreement)",
    "Schematics",
    "System topology, system control diagram",
    "Technical descriptions",
    "Termination drawing, termination details",
    "Time schedule, planning, work plan",
    "Typical drawing, sketch",
    "Verification document",
]

LEGACY_DEFAULT_DOCUMENT_TYPES = {
    "Supplier Invoice:SIN",
    "Customer Invoice:CIN",
    "Contract:CON",
    "Specification:SPE",
    "Funding Request:FUR",
    "Confirmation of Receipt:COR",
    "Funding Authorisation:FAU",
    "Drawing:DRW",
    "Report:RPT",
    "Manual:MAN",
    "Procedure:PRO",
    "Customer Demand:CDE",
    "Other:OTH",
    "Calendar:CAL",
    "Revision:REV",
    "Supplier Invoice",
    "Customer Invoice",
    "Contract",
    "Specification",
    "Funding Request",
    "Confirmation of Receipt",
    "Funding Authorisation",
    "Drawing",
    "Report",
    "Manual",
    "Procedure",
    "Customer Demand",
    "Other",
    "Calendar",
    "Revision",
}


def _normalize_project_document_types(current):
    values = [str(item).strip() for item in (current or []) if str(item).strip()]
    customs = []
    for item in values:
        label = item.split(":")[0].strip()
        if item in LEGACY_DEFAULT_DOCUMENT_TYPES or label in LEGACY_DEFAULT_DOCUMENT_TYPES:
            continue
        if item not in NEW_PROJECT_DOCUMENT_TYPES and label not in NEW_PROJECT_DOCUMENT_TYPES:
            customs.append(item)

    ordered = list(NEW_PROJECT_DOCUMENT_TYPES)
    seen = set(ordered)
    for custom in customs:
        if custom not in seen:
            ordered.append(custom)
            seen.add(custom)
    return ordered


def replace_project_document_types(apps, schema_editor):
    model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in model._meta.get_fields()}
    if "document_types" not in field_names:
        return

    to_update = []
    for obj in model.objects.all().only("id", "document_types"):
        current = list(getattr(obj, "document_types", []) or [])
        updated = _normalize_project_document_types(current)
        if updated != current:
            obj.document_types = updated
            to_update.append(obj)

    if to_update:
        model.objects.bulk_update(to_update, ["document_types"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0146_remove_ecpmpconfig_general_tasks"),
    ]

    operations = [
        migrations.RunPython(
            replace_project_document_types,
            migrations.RunPython.noop,
        ),
    ]
