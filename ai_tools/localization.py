PROJECT_TYPE_TRANSLATIONS = {
    # Legacy Energy Consultant defaults (kept for existing projects)
    "offer": {
        "en": "Offer / Quotation / Proposal",
        "de": "Angebot",
        "fr": "Offre / Devis / Proposition",
    },
    "isfp": {
        "en": "Individual Renovation Roadmap",
        "de": "iSFP",
        "fr": "Plan individuel de rénovation énergétique",
    },
    "roof_renovation": {
        "en": "Roof renovation / Roof refurbishment",
        "de": "Dachsanierung",
        "fr": "Rénovation / Réfection de toiture",
    },
    "other_project": {
        "en": "Other Project",
        "de": "Anderes Projekt",
        "fr": "Autre projet",
    },
    # Industrial classification defaults
    "engineering_design_studies": {
        "en": "Engineering, Design & Studies",
        "de": "Ingenieurwesen, Konstruktion & Studien",
        "fr": "Ingénierie, Conception & Études",
    },
    "epc_turnkey_project": {
        "en": "EPC / Turnkey Project",
        "de": "EPC- / Schlüsselfertiges Projekt",
        "fr": "Projet EPC / Clé en main",
    },
    "construction_installation": {
        "en": "Construction & Installation",
        "de": "Bau & Installation",
        "fr": "Construction & Installation",
    },
    "commissioning_startup": {
        "en": "Commissioning & Start-up",
        "de": "Inbetriebnahme & Anfahren",
        "fr": "Mise en service & Démarrage",
    },
    "operations_maintenance": {
        "en": "Operations & Maintenance",
        "de": "Betrieb & Instandhaltung",
        "fr": "Exploitation & Maintenance",
    },
    "industrial_improvement": {
        "en": "Industrial Improvement",
        "de": "Industrielle Leistungsverbesserung",
        "fr": "Amélioration de la performance industrielle",
    },
    "digital_data_it_project": {
        "en": "Digital, Data & IT Project",
        "de": "Digital-, Daten- & IT-Projekt",
        "fr": "Projet Digital, Data & Informatique",
    },
    "transformation_change": {
        "en": "Transformation & Change",
        "de": "Transformation & Change Management",
        "fr": "Transformation & Conduite du changement",
    },
    "energy_transition_project": {
        "en": "Energy & Transition Project",
        "de": "Energie- & Transitionsprojekt",
        "fr": "Projet Énergie & Transition énergétique",
    },
    "infrastructure_project": {
        "en": "Infrastructure Project",
        "de": "Infrastrukturprojekt",
        "fr": "Projet d'Infrastructure",
    },
    "program_multi_project": {
        "en": "Program / Multi-Project",
        "de": "Programm / Multi-Projekt",
        "fr": "Programme / Multi-projets",
    },
    "compliance_regulatory_project": {
        "en": "Compliance & Regulatory Project",
        "de": "Compliance- & Regulierungsprojekt",
        "fr": "Projet Conformité & Réglementation",
    },
}


PROJECT_TYPE_ALIASES = {
    alias.casefold(): key
    for key, translations in PROJECT_TYPE_TRANSLATIONS.items()
    for alias in translations.values()
}
PROJECT_TYPE_ALIASES.update(
    {
        "offer / proposal": "offer",
        "offre / proposition": "offer",
        "engineering, design & studies": "engineering_design_studies",
        "epc / turnkey project": "epc_turnkey_project",
        "construction & installation": "construction_installation",
        "commissioning & start-up": "commissioning_startup",
        "operations & maintenance": "operations_maintenance",
        "industrial improvement": "industrial_improvement",
        "digital, data & it project": "digital_data_it_project",
        "transformation & change": "transformation_change",
        "energy & transition project": "energy_transition_project",
        "infrastructure project": "infrastructure_project",
        "program / multi-project": "program_multi_project",
        "compliance & regulatory project": "compliance_regulatory_project",
    }
)

ACTION_ITEM_TRANSLATIONS = {
    "Anfrageperson zugeteilt": {
        "en": "Inquiry contact assigned",
        "fr": "Contact de la demande attribué",
    },
    "Kontakt mit Kunden aufnehmen": {
        "en": "Contact customer",
        "fr": "Contacter le client",
    },
    "Angebotsinhalte mit Kunden abgestimmt": {
        "en": "Offer details discussed with customer",
        "fr": "Contenu de l’offre discuté avec le client",
    },
    "Angebot erstellt und versendet": {
        "en": "Offer created and sent",
        "fr": "Offre créée et envoyée",
    },
    "Angebotsstatus beim Kunden erfragen": {
        "en": "Follow up on offer status with customer",
        "fr": "Relance client sur le statut de l’offre",
    },
    "Auftrag wurde erteilt": {
        "en": "Order awarded / Contract granted",
        "fr": "Commande confirmée / Contrat attribué",
    },
    "Daten bei Kunden abfragen": {
        "en": "Request client data for funding application",
        "fr": "Demander les données au client pour la demande de subvention",
    },
    "BAFA-Antrag im Portal per Vollmacht stellen / Kunde auf Antragsstellung hinweisen": {
        "en": "Submit application via portal with authorization / Inform client about application process",
        "fr": "Soumettre la demande via le portail dédié avec mandat / Informer le client sur la procédure",
    },
    "Pläne und weitere Informationen bei Kunden abfragen": {
        "en": "Request plans and additional information from client",
        "fr": "Demander les plans et informations complémentaires au client",
    },
    "Datenaufnahme für den Vor-Ort-Termin vorbereiten": {
        "en": "Prepare data collection for on-site visit",
        "fr": "Préparer la collecte de données pour la visite",
    },
    "Vort-Ort-Termin durchführen": {
        "en": "Conduct on-site visit",
        "fr": "Réaliser la visite sur site",
    },
    "IST-Haus in Energieberater-Software anlegen": {
        "en": "Enter current building data in energy audit software",
        "fr": "Saisir les données du bâtiment existant dans le logiciel d’audit énergétique",
    },
    "Sanierungskonzept festlegen": {
        "en": "Define renovation concept",
        "fr": "Définir le concept de rénovation",
    },
    "SOLL-Haus in Energieberater-Software anlegen": {
        "en": "Enter target building data in energy audit software",
        "fr": "Saisir les données du bâtiment cible dans le logiciel d’audit énergétique",
    },
    "iSFP an Kunde versenden": {
        "en": "Send renovation roadmap to client",
        "fr": "Envoyer le plan de rénovation au client",
    },
    "Abschlussgespräch vereinbaren": {
        "en": "Schedule final consultation",
        "fr": "Planifier l’entretien de clôture",
    },
    "Abschlussgespräch durchführen": {
        "en": "Conduct final consultation",
        "fr": "Réaliser l’entretien de clôture",
    },
    "Rechnung für iSFP stellen": {
        "en": "Issue invoice for renovation roadmap",
        "fr": "Émettre la facture pour le plan de rénovation",
    },
    "Verwendungsnachweis abrufen": {
        "en": "Retrieve proof of use documents from portal",
        "fr": "Récupérer les justificatifs d’utilisation via le portail",
    },
    "Verwendungsnachweis an Kunden senden": {
        "en": "Send proof of use documents to client",
        "fr": "Envoyer les justificatifs d’utilisation au client",
    },
    "Verwendungsnachweis im BAFA-Portal hinterlegen": {
        "en": "Upload proof of use documents to portal (BEIS / Ofgem)",
        "fr": "Déposer les justificatifs d’utilisation sur le portail (ADEME)",
    },
    "Fachunternehmerangebot: Technische Umsetzung und Förderbedingungen": {
        "en": "Contractor offer: Technical implementation and funding conditions OK?",
        "fr": "Offre de l’entrepreneur : mise en œuvre technique et conditions de financement OK ?",
    },
    "Fachunternehmerangebot: Technische Umsetzung und Förderbedingungen i-O-?": {
        "en": "Contractor offer: Technical implementation and funding conditions OK?",
        "fr": "Offre de l’entrepreneur : mise en œuvre technique et conditions de financement OK ?",
    },
    "Fachunternehmerangebot: Auflösende oder aufschiebende Bedingung enthlaten?": {
        "en": "Contractor offer: Includes resolutive or suspensive condition?",
        "fr": "Offre de l’entrepreneur : contient une condition résolutoire ou suspensive ?",
    },
    "Fachunternehmerangebot: Auflösende oder aufschiebende Bedingung enthalten?": {
        "en": "Contractor offer: Includes resolutive or suspensive condition?",
        "fr": "Offre de l’entrepreneur : contient une condition résolutoire ou suspensive ?",
    },
    "Fachunternehmerangebot: Umsetzungszeitraum im zukünftigen Bewilligungszeitraum (36 Monate) ?": {
        "en": "Contractor offer: Implementation period within future approval window (36 months)?",
        "fr": "Offre de l’entrepreneur : période de mise en œuvre dans la fenêtre d’approbation future (36 mois) ?",
    },
    "Vollständigkeit der Unterlagen prüfen (inkl- Vollmacht)": {
        "en": "Check completeness of documents (incl. power of attorney)",
        "fr": "Vérifier l’exhaustivité des documents (y compris la procuration)",
    },
    "Föderfähigkeit in Gänze gegeben?": {
        "en": "Confirm full eligibility for funding",
        "fr": "Vérifier l’éligibilité complète à la subvention",
    },
    "Technische Projektjbeschreibung (TPB) erstellen": {
        "en": "Create technical project description (TPD)",
        "fr": "Créer une description technique du projet (DTP)",
    },
    "BAFA-Hauptantrag stellen / durch Kunde oder per Vollmacht": {
        "en": "Submit main funding application via portal (by client or with authorization)",
        "fr": "Soumettre la demande principale via le portail (par le client ou avec mandat)",
    },
    "Eingangsbestätigung von Kunde abfragen / Eingangsbestätigung an Kunden schicken": {
        "en": "Request or send application receipt confirmation",
        "fr": "Demander ou envoyer la confirmation de réception de la demande au client",
    },
    "Rückmeldung BAFA bearbeiten (optional)": {
        "en": "Process feedback from funding agency (optional)",
        "fr": "Traiter les retours de l’organisme de subvention (optionnel)",
    },
    "Zuwendungsbescheid an Kunden schicken": {
        "en": "Send grant approval notice to client",
        "fr": "Envoyer l’avis d’attribution au client",
    },
    "Frist für Gültigkeit des Zuwendungsbescheids aktualisieren": {
        "en": "Update validity deadline of grant approval",
        "fr": "Mettre à jour la date de validité de l’avis d’attribution",
    },
    "Vor-Ort-Termin vereinbaren": {
        "en": "Schedule on-site inspection",
        "fr": "Planifier une inspection sur site",
    },
    "Vor-Ort-Termin durchführen": {
        "en": "Conduct on-site inspection",
        "fr": "Réaliser l’inspection sur site",
    },
    "Dokumentation ablegen (Fotos, …)": {
        "en": "Archive documentation (photos, etc.)",
        "fr": "Archiver la documentation (photos, etc.)",
    },
    "Fachunternehmer-Rechung und -erklärung abfragen": {
        "en": "Request contractor invoice and declaration",
        "fr": "Demander la facture et la déclaration de l’entrepreneur",
    },
    "Zahlungsnachweise abfragen": {
        "en": "Request payment proofs",
        "fr": "Demander les justificatifs de paiement",
    },
    "Dokumentation wahrheitsmäßige Angaben ablegen": {
        "en": "Archive truthful documentation",
        "fr": "Archiver la documentation véridique",
    },
    "Rechnung für Energieberatung stellen": {
        "en": "Issue invoice for energy audit",
        "fr": "Émettre la facture pour l’audit énergétique",
    },
    "Technischen Projektnachweis (TPN) erstellen": {
        "en": "Create technical project report",
        "fr": "Établir le rapport technique du projet",
    },
    "Verwendungsnachweis im BAFA-Portal erstellen // durch Kunde oder per Vollmacht": {
        "en": "Submit proof of use via portal (client or authorized representative)",
        "fr": "Soumettre le justificatif d’utilisation via le portail (client ou mandataire)",
    },
    "Dokumentation des gesamten Projektes ablegen (Aufbewahrungsfrist)": {
        "en": "Archive full project documentation (retention period)",
        "fr": "Archiver toute la documentation du projet (durée de conservation)",
    },
}


def localize_project_type(value, language):
    if not isinstance(value, str) or not value.strip():
        return value

    normalized_language = language if language in {"en", "de", "fr"} else "en"
    canonical_key = PROJECT_TYPE_ALIASES.get(value.strip().casefold())
    if not canonical_key:
        return value
    return PROJECT_TYPE_TRANSLATIONS[canonical_key][normalized_language]


def localize_project_types_in_result(value, language):
    if isinstance(value, list):
        return [localize_project_types_in_result(item, language) for item in value]
    if not isinstance(value, dict):
        return value

    localized = {}
    for key, item in value.items():
        if key == "project_type":
            localized[key] = localize_project_type(item, language)
        else:
            localized[key] = localize_project_types_in_result(item, language)
    return localized


def localize_action_item_name(value, language):
    if not isinstance(value, str) or not value.strip():
        return value
    normalized_language = language if language in {"en", "de", "fr"} else "en"
    if normalized_language == "de":
        return value
    translations = ACTION_ITEM_TRANSLATIONS.get(value.strip())
    return (translations or {}).get(normalized_language, value)


def localize_action_item_names_in_result(value, language):
    if not isinstance(value, dict):
        return value

    localized = dict(value)
    rows = []
    for row in value.get("rows") or []:
        localized_row = dict(row)
        localized_row["name"] = localize_action_item_name(
            row.get("name"),
            language,
        )
        rows.append(localized_row)
    if "rows" in value:
        localized["rows"] = rows

    sources = []
    for source in value.get("sources") or []:
        localized_source = dict(source)
        if source.get("type") == "action_item":
            localized_source["label"] = localize_action_item_name(
                source.get("label"),
                language,
            )
        sources.append(localized_source)
    if "sources" in value:
        localized["sources"] = sources
    return localized
