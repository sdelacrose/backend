from django.db import migrations

NEW_PROJECT_DOCUMENT_TYPES = [
    "3D model:DMO",
    "Architectural view:ARV",
    "Area classification drawing:ACD",
    "Calculation, curve, table:CAL",
    "Call for meeting, MoM, meeting presentation, etc:MOM",
    "Cause and effect chart:CEC",
    "Contract:CON",
    "Contractor Initiated Changes:CIC",
    "Contracting strategy:COS",
    "Correspondence:COR",
    "Correspondence of contract:COC",
    "Data sheet according to discipline:DTS",
    "Detail drawing:DED",
    "Diagram; engineering, functional, block, interconnection, nodal, circuit:DIA",
    "Drawing:DRW",
    "Equipment layout:EAY",
    "Estimate, budget:EST",
    "Fire protection drawing:FPD",
    "Frame agreement:FAG",
    "General and detailed specifications:SPC",
    "General arrangement drawing (guide drawing):GAD",
    "Heat and mass balance:HMB",
    "Heat tracing drawing:HTD",
    "Internal Due Diligence:IDD",
    "Isometric drawing:ISD",
    "ITB related (bid clarification):ITB",
    "Laboratory results & reports:LRR",
    "Layout of any kind (plot plan, site plan etc):LAY",
    "Letter:LET",
    "List of any kind, index, legend:LST",
    "Loading diagram, loading plan, structural loads:LOD",
    "Logic and sequence diagram:LSD",
    "Logistic:LOG",
    "Loop diagram:LOOD",
    "Maintenance information (eg Maintenance manual):MAI",
    "Material receiving report:MRR",
    "Material safety data sheet:MSD",
    "Material take-off:MTO",
    "Mechanical Completion and Commissioning documents:MCC",
    "Offer/quotation:OFF",
    "Operational information (eg Operating manual):OPI",
    "Org chart, functional descriptions etc:ORG",
    "Permitting and Licensing:PEL",
    "Piping and instrumentation diagram:PID",
    "Principal decisions:PCP",
    "Process flow diagram, utility flow diagram:PFD",
    "Project control (MPR, Cost report, close-out report, etc):PCO",
    "Project design criteria , Design basis:PDC",
    "Project procedure, work instruction, manual:MAN",
    "Quality Control Surv:QCS",
    "Report of any kind, according to discipline:RPT",
    "Request for information:RFI",
    "Requisition:REQ",
    "Sales agreement (Project Agreement):SAG",
    "Schematics:SCC",
    "Single Line Diagram:SLD",
    "Statutory Approval or Permission:SAP",
    "System topology, system control diagram:SYT",
    "Technical descriptions:TED",
    "Termination drawing, termination details:TER",
    "Time schedule, planning, work plan:SCH",
    "Typical drawing, sketch:TYD",
    "Verification document:VED",
    "Miscellaneous:MSC",
]

NEW_LABELS = {item.rsplit(":", 1)[0] for item in NEW_PROJECT_DOCUMENT_TYPES}
NEW_CODES = {
    item.rsplit(":", 1)[1]
    for item in NEW_PROJECT_DOCUMENT_TYPES
    if ":" in item
}

PREVIOUS_DEFAULT_LABELS = {
    "Project control (MPR, Cost report, close-out report, etc)",
    "Org chart, functional descriptions etc",
    "Operational information (eg Operating manual)",
    "Maintenance information (eg Maintenance manual)",
    "Layout of any kind (plot plan, site plan etc)",
    "Call for meeting, MoM, meeting presentation, etc",
    "Quality Control Surv",
    "Interim Payment App / Payment Notice",
    "Interim Payment App. / Payment Notice",
    "Architectural view",
    "Area classification drawing",
    "Cable schedule, pipe support and spring schedule",
    "Calculations, curves, tables",
    "Calculation, curve, table",
    "Call for meeting, MoM, meeting presentation, etc.",
    "Cause and effect chart",
    "Contract",
    "Contractor Initiated Changes",
    "Contracting strategy",
    "Correspondence",
    "Correspondence of contract",
    "D model",
    "3D model",
    "Data sheet, according to discipline",
    "Data sheet according to discipline",
    "Detail drawing",
    "Diagram; engineering, functional, block, interconnection, nodal, circuit",
    "Diagram: engineering, functional, block, interconnection, nodal, circuit",
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
    "Internal Due Diligence",
    "Isometric drawing",
    "ITB related (bid clarification)",
    "Laboratory results & reports",
    "Layout of any kind (plot plan, site plan etc.)",
    "Letter",
    "Lists of any kind, index, legend",
    "List of any kind, index, legend",
    "Loading diagram, loading plan, structural loads",
    "Logic and sequence diagram",
    "Logistic",
    "Loop diagram",
    "Maintenance information (e.g. Maintenance manual)",
    "Material receiving report",
    "Material safety data sheet",
    "Material take-off (MTO)",
    "Material take-off",
    "Mechanical Completion and Commissioning documents",
    "Miscellaneous (not covered by any specific document type above)",
    "Miscellaneous",
    "Miscellaneous ",
    "Offer/quotation",
    "Operational information (e.g. Operating manual)",
    "Org. chart, functional descriptions etc.",
    "Permitting and Licensing",
    "Piping and instrumentation diagram (PID)",
    "Piping and instrumentation diagram",
    "Principal decisions",
    "Process flow diagram, utility flow diagram",
    "Project control (MPR, Cost report, close-out report, etc.)",
    "Project design criteria; design basis, principal decisions, PCP",
    "Project design criteria , Design basis",
    "Project procedure, work instruction, manual",
    "Purchase order",
    "Quality Control Surv.",
    "Report of any kind, according to discipline",
    "Request for information",
    "Requisition",
    "Sales agreement (EAR or Project Agreement)",
    "Sales agreement (Project Agreement)",
    "Schematics",
    "Single Line Diagram",
    "Statutory Approval or Permission",
    "System topology, system control diagram",
    "Technical descriptions",
    "Termination drawing, termination details",
    "Time schedule, planning, work plan",
    "Typical drawing, sketch",
    "Verification document",
}


def _split_label_code(item):
    if ":" not in item:
        return item.strip(), ""
    label, code = item.rsplit(":", 1)
    return label.strip(), code.strip()


def _normalize_project_document_types(current):
    values = [str(item).strip() for item in (current or []) if str(item).strip()]
    customs = []
    for item in values:
        label, code = _split_label_code(item)
        if label in NEW_LABELS or label in PREVIOUS_DEFAULT_LABELS:
            continue
        if code and code in NEW_CODES:
            continue
        customs.append(item)

    ordered = list(NEW_PROJECT_DOCUMENT_TYPES)
    seen = set(ordered)
    seen_labels = set(NEW_LABELS)
    for custom in customs:
        custom_label, _code = _split_label_code(custom)
        if custom not in seen and custom_label not in seen_labels:
            ordered.append(custom)
            seen.add(custom)
            seen_labels.add(custom_label)
    return ordered


def update_project_document_types(apps, schema_editor):
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
        ("project_management", "0150_add_document_type_codes"),
    ]

    operations = [
        migrations.RunPython(
            update_project_document_types,
            migrations.RunPython.noop,
        ),
    ]
