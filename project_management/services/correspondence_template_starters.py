"""MAIL-1.5-B: the three starter templates (French, plain text) prepared for MAIL-1.5-D.

This module is DATA ONLY. Nothing imports it at runtime, nothing inserts it into any company, and no
migration or command uses it: MAIL-1.5-D decides how a company chooses to add them. They only use
variables that really exist (see correspondence_template.VARIABLES), carry no recipient, no
signature and no attachment, and every one stays freely editable once applied.

`recipient_name` needs a recipient contact and `parent_correspondence_reference` needs a previous
correspondence: when the context does not give one, the marker stays visible and the send is blocked
until the person fills or removes it.
"""

STARTER_TEMPLATES = (
    {
        "name": "Transmission documentaire",
        "description": "Transmettre des documents à un destinataire.",
        "language": "FR",
        "subject_template": "Transmission de documents - {{project_name}}",
        "body_template": (
            "Bonjour {{recipient_name}},\n"
            "\n"
            "Veuillez trouver ci-joint les documents relatifs au projet {{project_name}}.\n"
            "\n"
            "Merci de bien vouloir nous accuser réception de cet envoi et de nous faire part "
            "de vos éventuelles remarques.\n"
            "\n"
            "Cordialement,"
        ),
    },
    {
        "name": "Demande d'information",
        "description": "Solliciter des informations ou des documents auprès d'un destinataire.",
        "language": "FR",
        "subject_template": "Demande d'informations - {{project_name}}",
        "body_template": (
            "Bonjour {{recipient_name}},\n"
            "\n"
            "Dans le cadre du projet {{project_name}}, nous vous remercions de bien vouloir nous "
            "communiquer les informations ou documents suivants :\n"
            "\n"
            "- \n"
            "- \n"
            "\n"
            "Nous restons à votre disposition pour tout complément.\n"
            "\n"
            "Cordialement,"
        ),
    },
    {
        "name": "Relance",
        "description": "Relancer une correspondance précédente restée sans réponse.",
        "language": "FR",
        "subject_template": "Relance - {{parent_correspondence_reference}} - {{project_name}}",
        "body_template": (
            "Bonjour {{recipient_name}},\n"
            "\n"
            "Sauf erreur de notre part, nous n'avons pas encore reçu de réponse à notre "
            "correspondance {{parent_correspondence_reference}} relative au projet "
            "{{project_name}}.\n"
            "\n"
            "Nous vous remercions de bien vouloir nous répondre dans les meilleurs délais.\n"
            "\n"
            "Cordialement,"
        ),
    },
)
