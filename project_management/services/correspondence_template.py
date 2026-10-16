"""MAIL-1.5-B: validation and rendering of the e-mail templates (models/correspondence_template.py).

The "engine" is deliberately tiny and closed: a single regular-expression substitution of
`{{variable}}` markers taken from the fixed VARIABLES list. No template language, no attribute
access, no evaluation, no HTML: the text is plain text in, plain text out, exactly what the Microsoft
365 send sends (content type "Text").

Marker syntax (used for validating a template AND for blocking a send):

    {{ name }}      two opening braces, an identifier (letters, digits, underscore, not starting
                    with a digit), two closing braces, optional spaces inside.

Anything else -- single braces, `{{ 1 + 2 }}`, `{{}}`, JSON-looking text -- is ordinary text.

Unresolved markers are never replaced by an empty string: they stay in the rendered text, visible,
and are listed, and a text that still contains any marker cannot be sent (see send_correspondence).
"""
import re

from django.core.exceptions import ValidationError

VARIABLES = (
    "project_name",
    "project_code",
    "recipient_name",
    "recipient_company",
    "parent_correspondence_reference",
)

MARKER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Plain text only: an opening or closing tag, or a comment. `<jane@x.com>` and `a < b > c` are text.
_HTML_RE = re.compile(r"<\s*/?\s*[A-Za-z][A-Za-z0-9]*(\s[^<>]*)?/?\s*>|<!--")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # everything but \t \n \r

NAME_MAX = 255
SUBJECT_MAX = 500          # same limit as Correspondence.subject
BODY_MAX = 20000


class TemplateContextError(Exception):
    """The context given to apply a template is unusable; `errors` is a {field: message} dict."""

    def __init__(self, errors):
        super().__init__(errors)
        self.errors = errors


def find_markers(text):
    """The names of the `{{variable}}` markers in `text`, without duplicates, in order of appearance."""
    seen = []
    for name in MARKER_RE.findall(text or ""):
        if name not in seen:
            seen.append(name)
    return seen


def unresolved_marker_errors(*, subject, body):
    """For a send: {} when neither text holds a marker, else an error dict naming the field(s) and
    the markers found (known or not: a marker that survived to the send is, by definition, unresolved)."""
    errors = {}
    found = []
    for field, text in (("subject", subject), ("body", body)):
        names = find_markers(text)
        if names:
            found.extend(n for n in names if n not in found)
            errors[field] = (
                "This text still contains template placeholders that are not filled in: "
                + ", ".join("{{%s}}" % n for n in names)
                + ". Replace or remove them before sending."
            )
    if errors:
        errors["unresolved_variables"] = found
    return errors


def _check_text(field, text, *, max_length, single_line, errors):
    text = text or ""
    if not text.strip():
        errors[field] = "This field is required."
        return
    if len(text) > max_length:
        errors[field] = f"Ensure this field has no more than {max_length} characters."
        return
    if _CONTROL_RE.search(text) or (single_line and ("\n" in text or "\r" in text)):
        errors[field] = "This field contains characters that are not allowed."
        return
    if _HTML_RE.search(text):
        errors[field] = "HTML is not allowed: a template is plain text."
        return
    unknown = [n for n in find_markers(text) if n not in VARIABLES]
    if unknown:
        errors[field] = (
            "Unknown variable(s): " + ", ".join("{{%s}}" % n for n in unknown)
            + ". Allowed variables: " + ", ".join("{{%s}}" % n for n in VARIABLES) + "."
        )
        return
    if "{{" in MARKER_RE.sub("", text) or "}}" in MARKER_RE.sub("", text):
        errors[field] = (
            "Malformed placeholder: write a variable as {{name}}, or remove the double braces."
        )


def validate_template_fields(*, name, subject_template, body_template):
    """Raises django's ValidationError({field: message}) for a template that must not be stored."""
    errors = {}
    name = (name or "").strip()
    if not name:
        errors["name"] = "This field is required."
    elif len(name) > NAME_MAX:
        errors["name"] = f"Ensure this field has no more than {NAME_MAX} characters."
    elif _CONTROL_RE.search(name) or "\n" in name or "\r" in name:
        errors["name"] = "This field contains characters that are not allowed."
    _check_text("subject_template", subject_template, max_length=SUBJECT_MAX, single_line=True, errors=errors)
    _check_text("body_template", body_template, max_length=BODY_MAX, single_line=False, errors=errors)
    if errors:
        raise ValidationError(errors)


def _clean_value(value):
    """A resolved value, on one line; None when there is nothing usable (-> the marker stays)."""
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def render_text(text, values):
    """(rendered, resolved_names, unresolved_names). One pass over the markers: a value is never
    scanned again, so a value that itself looks like a marker is not expanded."""
    resolved, unresolved = [], []

    def substitute(match):
        name = match.group(1)
        value = _clean_value(values.get(name)) if name in VARIABLES else None
        if value is None:
            if name not in unresolved:
                unresolved.append(name)
            return match.group(0)
        if name not in resolved:
            resolved.append(name)
        return value

    return MARKER_RE.sub(substitute, text or ""), resolved, unresolved


def resolve_context(
    user, *, project_id=None, recipient_contact_id=None, recipient_company_id=None,
    parent_correspondence_id=None, visible_correspondences=None,
):
    """The values of the variables, from objects the user's own company owns -- nothing else is read.
    A missing, foreign or inconsistent object raises TemplateContextError (the same "Not found." for
    a foreign object as for a missing one: nothing about other companies is revealed).

    `visible_correspondences` is the queryset of the correspondences the user may see (the same scope
    as the correspondence list), required to resolve a parent."""
    from project_management.models import ContactCompany, ContactUser, Project

    company_id = getattr(user, "company_id", None)
    if not company_id:
        raise TemplateContextError({"detail": "Templates are available to company users only."})

    errors = {}
    project = company = contact = parent = None

    if project_id:
        project = Project.objects.filter(pk=project_id, company_profile_id=company_id).first()
        if project is None:
            errors["project"] = "Not found."
    if recipient_company_id:
        company = ContactCompany.objects.filter(
            pk=recipient_company_id, company_profile_id=company_id, is_deleted=False,
        ).first()
        if company is None:
            errors["recipient_company"] = "Not found."
    if recipient_contact_id:
        contact = ContactUser.objects.select_related("contact_company").filter(
            pk=recipient_contact_id, company_profile_id=company_id,
        ).first()
        if contact is None:
            errors["recipient_contact"] = "Not found."
    if parent_correspondence_id:
        parent = None
        if visible_correspondences is not None:
            parent = visible_correspondences.filter(pk=parent_correspondence_id).first()
        if parent is None:
            errors["parent_correspondence"] = "Not found."
    if errors:
        raise TemplateContextError(errors)

    if contact is not None and company is not None and contact.contact_company_id != company.id:
        errors["recipient_contact"] = "This contact does not belong to the selected company."
    if contact is not None and company is None and contact.contact_company_id:
        company = ContactCompany.objects.filter(
            pk=contact.contact_company_id, company_profile_id=company_id, is_deleted=False,
        ).first()
    if parent is not None:
        # The new correspondence lives in the project when one is given, else in the company library:
        # a parent from another scope would be a different thread.
        if (project.id if project else None) != parent.project_id:
            errors["parent_correspondence"] = "The previous correspondence is not in the same project."
    if errors:
        raise TemplateContextError(errors)

    values = {
        "project_name": project.project_name if project else None,
        "project_code": project.project_code if project else None,
        "recipient_name": f"{contact.name} {contact.surname}" if contact else None,
        "recipient_company": company.company_name if company else None,
        "parent_correspondence_reference": parent.display_reference if parent else None,
    }
    ids = {
        "project": project.id if project else None,
        "recipient_contact": contact.id if contact else None,
        "recipient_company": company.id if company else None,
        "parent_correspondence": parent.id if parent else None,
    }
    return values, ids


def apply_template(template, values):
    """The rendered subject and body of `template`, and which variables were / were not resolved.
    Reads nothing, writes nothing."""
    subject, subject_resolved, subject_unresolved = render_text(template.subject_template, values)
    body, body_resolved, body_unresolved = render_text(template.body_template, values)
    unresolved = list(subject_unresolved) + [n for n in body_unresolved if n not in subject_unresolved]
    resolved = list(subject_resolved) + [n for n in body_resolved if n not in subject_resolved]
    return {
        "subject": subject,
        "body": body,
        "resolved_variables": resolved,
        "unresolved_variables": unresolved,
        "has_unresolved": bool(unresolved),
    }
