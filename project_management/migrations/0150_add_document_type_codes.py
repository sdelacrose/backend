from django.db import migrations

NEW_PROJECT_DOCUMENT_TYPES = [
    "Architectural view:ARV",
    "Area classification drawing:ACD",
    "Cable schedule, pipe support and spring schedule:CSP",
    "Calculations, curves, tables:CCT",
    "Call for meeting, MoM, meeting presentation, etc.:CMM",
    "Cause and effect chart:CEC",
    "Contract:CON",
    "Contracting strategy:CST",
    "Correspondence:COR",
    "Correspondence of contract:COC",
    "D model:DMD",
    "Data sheet, according to discipline:DSD",
    "Detail drawing:DDR",
    "Diagram; engineering, functional, block, interconnection, nodal, circuit:DIA",
    "Drawing:DRW",
    "Equipment layout:EQL",
    "Estimate, budget:EBU",
    "Fire protection drawing:FPD",
    "Frame agreement:FRA",
    "General and detailed specifications:GDS",
    "General arrangement drawing (guide drawing):GAD",
    "Heat and mass balance:HMB",
    "Heat tracing drawing:HTD",
    "Internal Due Diligence (IDD):IDD",
    "Isometric drawing:ISO",
    "ITB related (bid clarification):ITB",
    "Laboratory results & reports:LAB",
    "Layout of any kind (plot plan, site plan etc.):LAY",
    "Lists of any kind, index, legend:LST",
    "Loading diagram, loading plan, structural loads:LOD",
    "Logic and sequence diagram:LSD",
    "Logistic:LOG",
    "Loop diagram:LPD",
    "Maintenance information (e.g. Maintenance manual):MIN",
    "Material receiving report:MRR",
    "Material take-off (MTO):MTO",
    "Mechanical Completion and Commissioning documents:MCC",
    "Miscellaneous (not covered by any specific document type above):MSC",
    "Offer/quotation:OFQ",
    "Operational information (e.g. Operating manual):OPI",
    "Org. chart, functional descriptions etc.:ORG",
    "Permitting and Licensing:PAL",
    "Piping and instrumentation diagram (PID):PID",
    "Process flow diagram, utility flow diagram:PFD",
    "Project control (MPR, Cost report, close-out report, etc.):PCT",
    "Project design criteria; design basis, principal decisions, PCP:PDC",
    "Project procedure, work instruction, manual:PPM",
    "Purchase order:PO",
    "Report of any kind, according to discipline:RPT",
    "Requisition:REQ",
    "Sales agreement (EAR or Project Agreement):SAG",
    "Schematics:SCH",
    "System topology, system control diagram:STD",
    "Technical descriptions:TDE",
    "Termination drawing, termination details:TDD",
    "Time schedule, planning, work plan:TSP",
    "Typical drawing, sketch:TYP",
    "Verification document:VER",
]

NEW_LABELS = {item.split(":")[0] for item in NEW_PROJECT_DOCUMENT_TYPES}


def _normalize_project_document_types(current):
    values = [str(item).strip() for item in (current or []) if str(item).strip()]
    customs = []
    for item in values:
        label = item.split(":")[0].strip()
        if label in NEW_LABELS:
            continue
        customs.append(item)

    ordered = list(NEW_PROJECT_DOCUMENT_TYPES)
    seen = set(ordered)
    seen_labels = set(NEW_LABELS)
    for custom in customs:
        custom_label = custom.split(":")[0].strip()
        if custom not in seen and custom_label not in seen_labels:
            ordered.append(custom)
            seen.add(custom)
            seen_labels.add(custom_label)
    return ordered


def add_project_document_type_codes(apps, schema_editor):
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
        ("project_management", "0149_replace_team_members_roles"),
    ]

    operations = [
        migrations.RunPython(
            add_project_document_type_codes,
            migrations.RunPython.noop,
        ),
    ]
