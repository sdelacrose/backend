from project_management.models import Folder


SYSTEM_FOLDER_NAMES = {
    "proposal": {
        "en": "Proposal",
        "de": "Angebot",
        "fr": "Offre",
    },
    "requirements_specification": {
        "en": "Requirements Specification",
        "de": "Lastenheft",
        "fr": "Cahier des charges",
    },
}


def normalized_locale(locale):
    locale = (locale or "en").lower().split("-", 1)[0]
    return locale if locale in {"en", "de", "fr"} else "en"


def ensure_project_content_folders(project, locale="en", dry_run=False):
    locale = normalized_locale(locale)
    root = Folder.objects.filter(project=project, parent__isnull=True).first()
    if not root:
        if dry_run:
            return [{"system_key": key, "created": True} for key in SYSTEM_FOLDER_NAMES]
        root = Folder.objects.create(
            project=project,
            name=f"{project.id}_root_folder",
        )

    results = []
    for system_key, names in SYSTEM_FOLDER_NAMES.items():
        folder = Folder.objects.filter(project=project, system_key=system_key).first()
        created = folder is None
        if created and not dry_run:
            folder = Folder.objects.create(
                project=project,
                parent=root,
                name=names[locale],
                system_key=system_key,
            )
        results.append(
            {
                "system_key": system_key,
                "name": names[locale],
                "created": created,
                "folder_id": folder.id if folder else None,
            }
        )
    return results


def get_project_content_folder(project, kind, locale="en"):
    system_key = "proposal" if kind == "offer" else "requirements_specification"
    ensure_project_content_folders(project, locale)
    return Folder.objects.get(project=project, system_key=system_key)
