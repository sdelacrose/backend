from models.document_control_configuration import get_document_control_configuration


def get_document_control_settings(document):
    """Resolved document_control_configuration that applies to a Document.

    A project document uses its project's ECPMPConfig (copied at project creation, no runtime
    inheritance). A document without a project gets the canonical defaults.
    """
    from project_management.models import ECPMPConfig

    project_id = document.folder.project_id if document.folder_id else None
    config = ECPMPConfig.objects.filter(project_id=project_id).first() if project_id else None
    return get_document_control_configuration(config)
