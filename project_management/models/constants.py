from copy import deepcopy

from models.constants import CUSTOMER_DEMAND_DOCUMENT_TYPE


DEFAULT_FOLDERS = [
    "project information",
    "client correspondence",
    "other correspondence",
    "meeting records",
    "invoices",
    "site_reports"
]
COLLABORATIVE_SOLUTION = ["Energy Consultant Solution", "Enterprise Solution"]
SPECIAL_SOLUTION = ["Core Solution"]

ECPMP_CONTRACT_TYPES = [
    "Fixed Price",
    "Time and Materials",
    "EPC/EPCM",
    "Subcontract",
    "Framework Agreement",
    "Professional Services",
    "Technical Assistance (TA)",
    "Turnkey",
    "Call-Off Contract",
    "Other",
]
ECPMP_INTERNATIONAL_STANDARDS = [
    "FIDIC Red Book",
    "FIDIC Yellow Book",
    "FIDIC Silver Book",
    "FIDIC White Book",
    "NEC3",
    "NEC4",
]
ECPMP_MAJOR_PROJECT_MODELS = [
    "BOOT",
    "BOT",
    "PPP",
    "PFI",
]
# Backward-compatible alias for older code still referencing the combined list.
ECPMP_CONTRACT_STANDARDS = ECPMP_INTERNATIONAL_STANDARDS + ECPMP_MAJOR_PROJECT_MODELS
ECPMP_COMMERCIAL_MODELS = [
    "Fixed Price Model",
    "Time & Materials",
    "Cost Reimbursable",
    "Target Cost",
    "Pain / Gain Share",
]
ECPMP_SUPPORTING_AGREEMENTS = [
    "NDA (Non-Disclosure Agreement)",
    "Framework Agreement",
    "Consortium Agreement",
    "Joint Venture Agreement",
    "Contract Amendment (Addendum)",
]
ECPMP_TEAM_MEMBERS_ROLES = [
    "consultant",
    "project manager",
    "customer",
    "supplier",
    "notified body",
]
ECPMP_AREA_OF_EXPERTISE = ["HVAC services company", "carpenter", "energy consultancy"]
ECPMP_FUNDING_STRATEGY = [
                "GEG Energy efficient Building 40", "GEG Energy efficient Building 40 EE",
                "GEG Energy efficient Building 55", "GEG Energy efficient Building 55 EE",
                "GEG Energy efficient Building 70", "GEG Energy efficient Building 70 EE",
                "GEG Energy efficient Building 85", "GEG Energy efficient Building 85 EE",
                "BEG single" ,"BEG EinzelmaBnahme + iSFP", "iSPF"
            ]
ECPMP_STATUS_OPTIONS = ["open:#99dce8", "in progress:#fff3ad", "closed:#bdefe0"]
ECPMP_DEMAND_STATUS_OPTIONS = ["open:#99dce8", "in progress:#fff3ad", "closed:#bdefe0"]
ECPMP_REQUIREMENT_STATUS_OPTIONS = ["open:#99dce8", "in progress:#fff3ad", "closed:#bdefe0"]
ECPMP_BOM_STATUS_OPTIONS = ["open:#99dce8", "in progress:#fff3ad", "closed:#bdefe0"]
ECPMP_ACCEPTANCE_STATUS_OPTIONS = [
    "Draft:#e5e7eb",
    "Planned:#dbeafe",
    "Ready:#cffafe",
    "In Progress:#fef3c7",
    "Passed:#d1fae5",
    "Failed:#fee2e2",
    "Blocked:#fed7aa",
    "Waived:#ede9fe",
]
ECPMP_REMINDER_SETTINGS = ["none", "1 day", "2 days", "1 week", "2 weeks"]
ECPMP_INVOICE_STATUS = ["open", "due", "overdue", "paid"]
ECPMP_EXPENSE_TYPES = [
    "milage", "car", "hotel", "transport", "parking", "gasoline",
    "food", "taxi", "phone", "hardware", "software", "other",
]
ECPMP_PURCHASE_ORDER_STATUS = ["draft", "under_approval", "approved", "issued", "closed", "cancelled"]

# Fixed PO status lists (Energy Consultant / GC UI); not configurable per project.
INCOMING_PURCHASE_ORDER_STATUSES = [
    "Received",
    "Pending Invoice",
    "Partially Invoiced",
    "Fully Invoiced",
    "Fully Paid",
    "Closed",
]
OUTGOING_PURCHASE_ORDER_STATUSES = [
    "Sent",
    "Pending Invoice Receipt",
    "Partially Invoiced",
    "Fully Invoiced",
    "Fully Paid",
    "Closed",
]

GC_PROJECT_TYPES = [
    "Engineering, Design & Studies",
    "EPC / Turnkey Project",
    "Construction & Installation",
    "Commissioning & Start-up",
    "Operations & Maintenance",
    "Industrial Improvement",
    "Digital, Data & IT Project",
    "Transformation & Change",
    "Energy & Transition Project",
    "Infrastructure Project",
    "Program / Multi-Project",
    "Compliance & Regulatory Project",
]
LEGACY_GC_PROJECT_TYPES = ["Angebot", "iSFP", "Dachsanierung", "Other Project"]
GC_PROJECT_PRIORITY_LEVELS = ["Very Low","Low", "Medium", "High", "Very High"]
GC_DOCUMENT_STATUSES = [
    "Draft",
    "Under review",
    "Submitted",
    "Approved",
    "Sent",
    "To be updated",
    "For construction (IFC)",
    "Issued",
    "Revised",
    "Final",
    "As-Built",
    "On Hold",
    "Cancelled",
    "Archived",
    "Other",
]
GC_DOCUMENT_INTERNAL_REVIEW_STATUSES = [
    "In progress",
    "Approved",
    "Commented",
    "Rejected",
]
GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES = [
    "In progress",
    "Approved",
    "Reviewed",
    "Commented",
    "Rejected",
    "For information",
    "Superseded",
]
GC_TRANSMITTAL_CATEGORIES = [
    "Not sent",
    "Available",
    "For review",
    "For information",
]
GC_PROJECT_SERVICE_TYPES = []

# Document Numbering System Constants
LEGACY_GC_DOCUMENT_TYPES = [
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
    CUSTOMER_DEMAND_DOCUMENT_TYPE,
    "Other:OTH",
    "Calendar:CAL",
    "Revision:REV",
]
GC_COMPANY_DOCUMENT_TYPES = [
    "Bonds:BON",
    "Cash Flow Statement:SFS",
    "Change/Variation:CVA",
    "Claims/extensions:CEX",
    "Collateral Warranty:CW",
    "Contractor Initiated Changes:CIC",
    "Contractual Correspondence and Notices:CCN",
    "Coordination Meeting:COM",
    "Drawing:DRW",
    "Employer's Instructions:EIN",
    "Estimate, budget:EST",
    "Final Payment:FPA",
    "General:GEN",
    "Hand-over:HOV",
    "Insurance:INS",
    "Interim Payment App / Payment Notice:INP",
    "Letter:LET",
    "List of any kind, index, legend:LST",
    "Loss and Expense Claim:LEC",
    "Minutes of meeting:MOM",
    "Programme:PGM",
    "Progress Report:PRE",
    "Punch list:PUL",
    "Purchase order:PO",
    "Quality Control Surv:QCS",
    "Rectification Period:REP",
    "Schedule:SCH",
    "Statutory Approval or Permission:SAP",
    "Steering Committee Meeting:SCM",
    CUSTOMER_DEMAND_DOCUMENT_TYPE,
    "Other:OTH",
]
GC_PROJECT_DOCUMENT_TYPES = [
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
    CUSTOMER_DEMAND_DOCUMENT_TYPE,
    "Other:OTH",
]
# Prefer GC_COMPANY_DOCUMENT_TYPES / GC_PROJECT_DOCUMENT_TYPES for new code.
GC_DOCUMENT_TYPES = GC_COMPANY_DOCUMENT_TYPES


GC_DOCUMENT_CATEGORIES = ["General:GAD", "Design:DTS", "Technical:TMP",  "Manual:MAN", "Procedure:PRO", "Other:OTH"]
GC_DOCUMENT_DISCIPLINES = ["Electrical:EL", "Mechanical:ME", "Hydraulic:HY", "Architectural:AR", "Structural:ST", "Plumbing:PL", "HVAC:HV", "Other:OTH"]

PROJECT_MILESTONE_ACTION_CONFIG = {
    "Angebot": {
        "milestones": [
            {
                "name": "Anfrage",
                "due_date": 5,
                "action_items": [
                    {
                        "name": "Anfrageperson zugeteilt",
                        "due_date": 2
                    },
                    {
                        "name": "Kontakt mit Kunden aufnehmen",
                        "due_date": 3
                    }
                ]
            },
            {
                "name": "Angebot",
                "due_date": 10,
                "action_items": [
                    {
                        "name": "Angebotsinhalte mit Kunden abgestimmt",
                        "due_date": 5
                    },
                    {
                        "name": "Angebot erstellt und versendet",
                        "due_date": 8
                    }
                ]
            },
            {
                "name": "Auftrag",
                "due_date": 30,
                "action_items": [
                    {
                        "name": "Angebotsstatus beim Kunden erfragen",
                        "due_date": 20
                    },
                    {
                        "name": "Auftrag wurde erteilt",
                        "due_date": 25
                    }
                ]
            }
        ]
    },
    "iSFP": {
        "milestones": [
            {
                "name": "BAFA-Förderantrag für Energieberatung stellen",
                "due_date": 10,
                "action_items": [
                    {
                        "name": "Daten bei Kunden abfragen",
                        "due_date": 5
                    },
                    {
                        "name": "BAFA-Antrag im Portal per Vollmacht stellen / Kunde auf Antragsstellung hinweisen",
                        "due_date": 8
                    },
                ]
            },
            {
                "name": "Daten für Energieberatung sammeln",
                "due_date": 15,
                "action_items": [
                    {
                        "name": "Pläne und weitere Informationen bei Kunden abfragen",
                        "due_date": 5
                    },
                    {
                        "name": "Vor-Ort-Termin vereinbaren",
                        "due_date": 8
                    },
                    {
                        "name": "Datenaufnahme für den Vor-Ort-Termin vorbereiten",
                        "due_date": 10
                    },
                    {
                        "name": "Vort-Ort-Termin durchführen",
                        "due_date": 12
                    },
                ]
            },
            {
                "name": "Sanierungsfahrplan erstellen",
                "due_date": 20,
                "action_items": [
                    {
                        "name": "IST-Haus in Energieberater-Software anlegen",
                        "due_date": 15
                    },
                    {
                        "name": "Sanierungskonzept festlegen",
                        "due_date": 18
                    },
                    {
                        "name": "SOLL-Haus in Energieberater-Software anlegen",
                        "due_date": 19
                    }
                ]
            },
            {
                "name": "Energieberatung mit Kunden abschließen",
                "due_date": 25,
                "action_items": [
                    {
                        "name": "iSFP an Kunde versenden",
                        "due_date": 20
                    },
                    {
                        "name": "Abschlussgespräch vereinbaren",
                        "due_date": 20
                    },
                    {
                        "name": "Abschlussgespräch durchführen",
                        "due_date": 25
                    },
                    {
                        "name": "Rechnung für iSFP stellen",
                        "due_date": 25
                    }
                ]
            },
            {
                "name": "BAFA-Förderantrag abschließen",
                "due_date": 140,
                "action_items": [
                    {
                        "name": "Verwendungsnachweis abrufen",
                        "due_date": 100
                    },
                    {
                        "name": "Verwendungsnachweis an Kunden senden",
                        "due_date": 100
                    },
                    {
                        "name": "Verwendungsnachweis im BAFA-Portal hinterlegen",
                        "due_date": 105
                    }
                ]
            }
        ]
    },
    "Dachsanierung": {
        "milestones": [
            {
                "name": "BAFA-Förderantrag vorbereiten",
                "due_date": 10,
                "action_items": [
                    {
                        "name": "Daten bei Kunden abfragen",
                        "due_date": 2
                    },
                    {
                        "name": "Fachunternehmerangebot: Technische Umsetzung und Förderbedingungen i.O.?",
                        "due_date": 5
                    },
                    {
                        "name": "Fachunternehmerangebot: Auflösende oder aufschiebende Bedingung enthlaten?",
                        "due_date": 8
                    },
                    {
                        "name": "Fachunternehmerangebot: Umsetzungszeitraum im zukünftigen Bewilligungszeitraum (36 Monate) ?",
                        "due_date": 8
                    },
                    {
                        "name": "Vollständigkeit der Unterlagen prüfen (inkl. Vollmacht)",
                        "due_date": 9
                    },
                    {
                        "name": "Föderfähigkeit in Gänze gegeben?",
                        "due_date": 10
                    }
                ]
            },
            {
                "name": "BAFA-Förderantrag stellen",
                "due_date": 20,
                "action_items": [
                    {
                        "name": "Technische Projektjbeschreibung (TPB) erstellen",
                        "due_date": 12
                    },
                    {
                        "name": "BAFA-Hauptantrag stellen / durch Kunde oder per Vollmacht",
                        "due_date": 13
                    },
                    {
                        "name": "Eingangsbestätigung von Kunde abfragen / Eingangsbestätigung an Kunden schicken",
                        "due_date": 18
                    }
                ]
            },
            {
                "name": "Zuwendungsbescheid verarbeiten",
                "due_date": 30,
                "action_items": [
                    {
                        "name": "Rückmeldung BAFA bearbeiten (optional)",
                        "due_date": 23
                    },
                    {
                        "name": "Zuwendungsbescheid an Kunden schicken",
                        "due_date": 25
                    },
                    {
                        "name": "Frist für Gültigkeit des Zuwendungsbescheids aktualisieren",
                        "due_date": 25
                    }
                ]
            },
            {
                "name": "Vor-Ort-Stichpoben durchführen",
                "due_date": 100,
                "action_items": [
                    {
                        "name": "Vor-Ort-Termin vereinbaren",
                        "due_date": 30
                    },
                    {
                        "name": "Vor-Ort-Termin durchführen",
                        "due_date": 40
                    },
                    {
                        "name": "Dokumentation ablegen (Fotos, …)",
                        "due_date": 45
                    }
                ]
            },
            {
                "name": "BAFA-Antragsabschluss vorbereiten",
                "due_date": 200,
                "action_items": [
                    {
                        "name": "Fachunternehmer-Rechung und -erklärung abfragen",
                        "due_date": 100
                    },
                    {
                        "name": "Zahlungsnachweise abfragen",
                        "due_date": 100
                    },
                    {
                        "name": "Dokumentation wahrheitsmäßige Angaben ablegen",
                        "due_date": 150
                    },
                    {
                        "name": "Rechnung für Energieberatung stellen",
                        "due_date": 100
                    }
                ]
            },
            {
                "name": "BAFA-Verwendungsnachweis erstellen",
                "due_date": 250,
                "action_items": [
                    {
                        "name": "Technischen Projektnachweis (TPN) erstellen",
                        "due_date": 230
                    },
                    {
                        "name": "Verwendungsnachweis im BAFA-Portal erstellen // durch Kunde oder per Vollmacht",
                        "due_date": 230
                    },
                    {
                        "name": "Dokumentation des gesamten Projektes ablegen (Aufbewahrungsfrist)",
                        "due_date": 230
                    }
                ]
            }
        ]
    }
}


SUPPORTED_PROJECT_TEMPLATE_LOCALES = ("en", "de", "fr")

PROJECT_TEMPLATE_LOCALE_ALIASES = {
    "de": "de",
    "de-de": "de",
    "english": "en",
    "en": "en",
    "en-gb": "en",
    "en-us": "en",
    "french": "fr",
    "fr": "fr",
    "fr-fr": "fr",
    "german": "de",
}

PROJECT_PHASE_TEMPLATE_TRANSLATIONS = {
    "default.project_phase": {
        "de": "Standardprojektphase",
        "en": "Default Project Phase",
        "fr": "Phase de projet par défaut",
    },
    "offer.inquiry": {
        "de": "Anfrage",
        "en": "Inquiry",
        "fr": "Demande initiale",
    },
    "offer.proposal": {
        "de": "Angebot",
        "en": "Offer / Proposal",
        "fr": "Offre / Proposition",
    },
    "offer.order": {
        "de": "Auftrag",
        "en": "Order / Contract",
        "fr": "Commande / Contrat",
    },
    "isfp.submit_funding_application": {
        "de": "BAFA-Förderantrag für Energieberatung stellen",
        "en": "Submit funding application to BEIS or Ofgem for energy audit",
        "fr": "Déposer une demande de subvention auprès de l’ADEME pour audit énergétique",
    },
    "isfp.collect_energy_audit_data": {
        "de": "Daten für Energieberatung sammeln",
        "en": "Collect data for energy audit",
        "fr": "Collecter les données pour l’audit énergétique",
    },
    "isfp.create_renovation_roadmap": {
        "de": "Sanierungsfahrplan erstellen",
        "en": "Develop renovation roadmap",
        "fr": "Élaborer le plan de rénovation énergétique",
    },
    "isfp.complete_energy_consultation": {
        "de": "Energieberatung mit Kunden abschließen",
        "en": "Finalize energy audit with client",
        "fr": "Finaliser l’audit énergétique avec le client",
    },
    "isfp.complete_funding_application": {
        "de": "BAFA-Förderantrag abschließen",
        "en": "Complete funding process with BEIS / Ofgem",
        "fr": "Clôturer la procédure de subvention auprès de l’ADEME",
    },
    "roof.prepare_funding_application": {
        "de": "BAFA-Förderantrag vorbereiten",
        "en": "Prepare funding application for energy audit (BEIS / Ofgem)",
        "fr": "Préparer la demande de subvention pour audit énergétique (ADEME)",
    },
    "roof.submit_funding_application": {
        "de": "BAFA-Förderantrag stellen",
        "en": "Submit funding application via portal (BEIS / Ofgem)",
        "fr": "Soumettre la demande de subvention via le portail (ADEME)",
    },
    "roof.process_grant_approval": {
        "de": "Zuwendungsbescheid verarbeiten",
        "en": "Process grant approval notice",
        "fr": "Traiter l’avis d’attribution de la subvention",
    },
    "roof.perform_on_site_samples": {
        "de": "Vor-Ort-Stichpoben durchführen",
        "en": "Conduct on-site sample inspections",
        "fr": "Réaliser des inspections ponctuelles sur site",
    },
    "roof.prepare_application_completion": {
        "de": "BAFA-Antragsabschluss vorbereiten",
        "en": "Prepare final documentation for funding closure",
        "fr": "Préparer les documents de clôture de la subvention",
    },
    "roof.create_proof_of_use": {
        "de": "BAFA-Verwendungsnachweis erstellen",
        "en": "Create proof of use documentation for funding (BEIS / Ofgem)",
        "fr": "Établir les justificatifs d’utilisation pour la subvention (ADEME)",
    },
}

PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS = {
    "offer.assign_inquiry_owner": {
        "de": "Anfrageperson zugeteilt",
        "en": "Inquiry owner assigned",
        "fr": "Responsable de la demande assigné",
    },
    "offer.contact_customer": {
        "de": "Kontakt mit Kunden aufnehmen",
        "en": "Contact customer",
        "fr": "Contacter le client",
    },
    "offer.align_offer_content": {
        "de": "Angebotsinhalte mit Kunden abgestimmt",
        "en": "Offer details discussed with customer",
        "fr": "Contenu de l’offre discuté avec le client",
    },
    "offer.create_and_send_offer": {
        "de": "Angebot erstellt und versendet",
        "en": "Offer created and sent",
        "fr": "Offre créée et envoyée",
    },
    "offer.follow_up_offer_status": {
        "de": "Angebotsstatus beim Kunden erfragen",
        "en": "Follow up on offer status with customer",
        "fr": "Relance client sur le statut de l’offre",
    },
    "offer.order_awarded": {
        "de": "Auftrag wurde erteilt",
        "en": "Order awarded / Contract granted",
        "fr": "Commande confirmée / Contrat attribué",
    },
    "common.request_customer_data": {
        "de": "Daten bei Kunden abfragen",
        "en": "Request client data for funding application",
        "fr": "Demander les données au client pour la demande de subvention",
    },
    "isfp.submit_authorized_application": {
        "de": "BAFA-Antrag im Portal per Vollmacht stellen / Kunde auf Antragsstellung hinweisen",
        "en": "Submit application via portal with authorization / Inform client about application process",
        "fr": "Soumettre la demande via le portail dédié avec mandat / Informer le client sur la procédure",
    },
    "isfp.request_plans": {
        "de": "Pläne und weitere Informationen bei Kunden abfragen",
        "en": "Request plans and additional information from client",
        "fr": "Demander les plans et informations complémentaires au client",
    },
    "common.schedule_on_site_appointment": {
        "de": "Vor-Ort-Termin vereinbaren",
        "en": "Schedule on-site inspection",
        "fr": "Planifier une inspection sur site",
    },
    "isfp.prepare_on_site_data": {
        "de": "Datenaufnahme für den Vor-Ort-Termin vorbereiten",
        "en": "Prepare data collection for on-site visit",
        "fr": "Préparer la collecte de données pour la visite",
    },
    "isfp.perform_on_site_visit": {
        "de": "Vort-Ort-Termin durchführen",
        "en": "Conduct on-site visit",
        "fr": "Réaliser la visite sur site",
    },
    "isfp.create_current_building": {
        "de": "IST-Haus in Energieberater-Software anlegen",
        "en": "Enter current building data in energy audit software",
        "fr": "Saisir les données du bâtiment existant dans le logiciel d’audit énergétique",
    },
    "isfp.define_renovation_concept": {
        "de": "Sanierungskonzept festlegen",
        "en": "Define renovation concept",
        "fr": "Définir le concept de rénovation",
    },
    "isfp.create_target_building": {
        "de": "SOLL-Haus in Energieberater-Software anlegen",
        "en": "Enter target building data in energy audit software",
        "fr": "Saisir les données du bâtiment cible dans le logiciel d’audit énergétique",
    },
    "isfp.send_roadmap": {
        "de": "iSFP an Kunde versenden",
        "en": "Send renovation roadmap to client",
        "fr": "Envoyer le plan de rénovation au client",
    },
    "isfp.schedule_final_consultation": {
        "de": "Abschlussgespräch vereinbaren",
        "en": "Schedule final consultation",
        "fr": "Planifier l’entretien de clôture",
    },
    "isfp.perform_final_consultation": {
        "de": "Abschlussgespräch durchführen",
        "en": "Conduct final consultation",
        "fr": "Réaliser l’entretien de clôture",
    },
    "isfp.invoice_roadmap": {
        "de": "Rechnung für iSFP stellen",
        "en": "Issue invoice for renovation roadmap",
        "fr": "Émettre la facture pour le plan de rénovation",
    },
    "isfp.retrieve_proof_of_use": {
        "de": "Verwendungsnachweis abrufen",
        "en": "Retrieve proof of use documents from portal",
        "fr": "Récupérer les justificatifs d’utilisation via le portail",
    },
    "isfp.send_proof_of_use": {
        "de": "Verwendungsnachweis an Kunden senden",
        "en": "Send proof of use documents to client",
        "fr": "Envoyer les justificatifs d’utilisation au client",
    },
    "isfp.upload_proof_of_use": {
        "de": "Verwendungsnachweis im BAFA-Portal hinterlegen",
        "en": "Upload proof of use documents to portal (BEIS / Ofgem)",
        "fr": "Déposer les justificatifs d’utilisation sur le portail (ADEME)",
    },
    "roof.validate_offer_implementation": {
        "de": "Fachunternehmerangebot: Technische Umsetzung und Förderbedingungen i.O.?",
        "en": "Contractor offer: Technical implementation and funding conditions OK?",
        "fr": "Offre de l’entrepreneur : mise en œuvre technique et conditions de financement OK ?",
    },
    "roof.validate_offer_condition": {
        "de": "Fachunternehmerangebot: Auflösende oder aufschiebende Bedingung enthlaten?",
        "en": "Contractor offer: Includes resolutive or suspensive condition?",
        "fr": "Offre de l’entrepreneur : contient une condition résolutoire ou suspensive ?",
    },
    "roof.validate_offer_period": {
        "de": "Fachunternehmerangebot: Umsetzungszeitraum im zukünftigen Bewilligungszeitraum (36 Monate) ?",
        "en": "Contractor offer: Implementation period within future approval window (36 months)?",
        "fr": "Offre de l’entrepreneur : période de mise en œuvre dans la fenêtre d’approbation future (36 mois) ?",
    },
    "roof.check_document_completeness": {
        "de": "Vollständigkeit der Unterlagen prüfen (inkl. Vollmacht)",
        "en": "Check completeness of documents (incl. power of attorney)",
        "fr": "Vérifier l’exhaustivité des documents (y compris la procuration)",
    },
    "roof.confirm_funding_eligibility": {
        "de": "Föderfähigkeit in Gänze gegeben?",
        "en": "Confirm full eligibility for funding",
        "fr": "Vérifier l’éligibilité complète à la subvention",
    },
    "roof.create_technical_project_description": {
        "de": "Technische Projektjbeschreibung (TPB) erstellen",
        "en": "Create technical project description (TPD)",
        "fr": "Créer une description technique du projet (DTP)",
    },
    "roof.submit_main_application": {
        "de": "BAFA-Hauptantrag stellen / durch Kunde oder per Vollmacht",
        "en": "Submit main funding application via portal (by client or with authorization)",
        "fr": "Soumettre la demande principale via le portail (par le client ou avec mandat)",
    },
    "roof.handle_receipt_confirmation": {
        "de": "Eingangsbestätigung von Kunde abfragen / Eingangsbestätigung an Kunden schicken",
        "en": "Request or send application receipt confirmation",
        "fr": "Demander ou envoyer la confirmation de réception de la demande au client",
    },
    "roof.process_bafa_feedback": {
        "de": "Rückmeldung BAFA bearbeiten (optional)",
        "en": "Process feedback from funding agency (optional)",
        "fr": "Traiter les retours de l’organisme de subvention (optionnel)",
    },
    "roof.send_grant_approval": {
        "de": "Zuwendungsbescheid an Kunden schicken",
        "en": "Send grant approval notice to client",
        "fr": "Envoyer l’avis d’attribution au client",
    },
    "roof.update_grant_validity": {
        "de": "Frist für Gültigkeit des Zuwendungsbescheids aktualisieren",
        "en": "Update validity deadline of grant approval",
        "fr": "Mettre à jour la date de validité de l’avis d’attribution",
    },
    "roof.perform_on_site_inspection": {
        "de": "Vor-Ort-Termin durchführen",
        "en": "Conduct on-site inspection",
        "fr": "Réaliser l’inspection sur site",
    },
    "roof.archive_site_documentation": {
        "de": "Dokumentation ablegen (Fotos, …)",
        "en": "Archive documentation (photos, etc.)",
        "fr": "Archiver la documentation (photos, etc.)",
    },
    "roof.request_contractor_documents": {
        "de": "Fachunternehmer-Rechung und -erklärung abfragen",
        "en": "Request contractor invoice and declaration",
        "fr": "Demander la facture et la déclaration de l’entrepreneur",
    },
    "roof.request_payment_proofs": {
        "de": "Zahlungsnachweise abfragen",
        "en": "Request payment proofs",
        "fr": "Demander les justificatifs de paiement",
    },
    "roof.archive_truthful_documentation": {
        "de": "Dokumentation wahrheitsmäßige Angaben ablegen",
        "en": "Archive truthful documentation",
        "fr": "Archiver la documentation véridique",
    },
    "roof.invoice_energy_consultation": {
        "de": "Rechnung für Energieberatung stellen",
        "en": "Issue invoice for energy audit",
        "fr": "Émettre la facture pour l’audit énergétique",
    },
    "roof.create_technical_project_report": {
        "de": "Technischen Projektnachweis (TPN) erstellen",
        "en": "Create technical project report",
        "fr": "Établir le rapport technique du projet",
    },
    "roof.create_portal_proof_of_use": {
        "de": "Verwendungsnachweis im BAFA-Portal erstellen // durch Kunde oder per Vollmacht",
        "en": "Submit proof of use via portal (client or authorized representative)",
        "fr": "Soumettre le justificatif d’utilisation via le portail (client ou mandataire)",
    },
    "roof.archive_project_documentation": {
        "de": "Dokumentation des gesamten Projektes ablegen (Aufbewahrungsfrist)",
        "en": "Archive full project documentation (retention period)",
        "fr": "Archiver toute la documentation du projet (durée de conservation)",
    },
}


def normalize_project_template_locale(locale):
    normalized_locale = str(locale or "").strip().lower().replace("_", "-")
    return PROJECT_TEMPLATE_LOCALE_ALIASES.get(normalized_locale, "en")


def resolve_project_template_locale(locale, preferred_language=None):
    normalized_locale = str(locale or "").strip().lower().replace("_", "-")
    requested_locale = PROJECT_TEMPLATE_LOCALE_ALIASES.get(normalized_locale)
    if requested_locale:
        return requested_locale

    normalized_preference = (
        str(preferred_language or "").strip().lower().replace("_", "-")
    )
    return PROJECT_TEMPLATE_LOCALE_ALIASES.get(normalized_preference, "en")


def get_localized_project_template(project_type, locale):
    project_template = PROJECT_MILESTONE_ACTION_CONFIG.get(project_type)
    if not project_template:
        return None

    locale = normalize_project_template_locale(locale)
    localized_template = deepcopy(project_template)
    phase_templates_by_german_name = {
        translations["de"]: (template_key, translations)
        for template_key, translations in PROJECT_PHASE_TEMPLATE_TRANSLATIONS.items()
        if template_key != "default.project_phase"
    }
    action_templates_by_german_name = {
        translations["de"]: (template_key, translations)
        for template_key, translations in PROJECT_ACTION_ITEM_TEMPLATE_TRANSLATIONS.items()
    }

    for phase in localized_template["milestones"]:
        phase_key, phase_translations = phase_templates_by_german_name[phase["name"]]
        phase["template_key"] = phase_key
        phase["name"] = phase_translations[locale]

        for action_item in phase["action_items"]:
            action_key, action_translations = action_templates_by_german_name[
                action_item["name"]
            ]
            action_item["template_key"] = action_key
            action_item["name"] = action_translations[locale]

    return localized_template


def get_default_project_phase_name(locale):
    locale = normalize_project_template_locale(locale)
    return PROJECT_PHASE_TEMPLATE_TRANSLATIONS["default.project_phase"][locale]
