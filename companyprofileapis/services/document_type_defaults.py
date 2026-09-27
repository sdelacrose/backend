"""LOT 2D-12: the ``include_defaults=true`` merge for ``document_types`` only.

The existing ``include_defaults`` mechanism (CompanyConfigSerializer / ECPMPCONFIGSerializer
``to_representation``) *annotates* the stored values of most configuration keys with
``is_default`` but never adds a canonical entry that is missing from storage. For most keys
that is fine (an empty list there genuinely means "nothing configured"), but for
``document_types`` an empty stored list must still let the user pick from the canonical
"Label:CODE" set -- otherwise the Document Type selector has nothing to offer and the
permanent Document reference can never resolve a type code. This is the single, targeted fix:
missing canonical entries are appended before annotation; existing (including custom) entries
are preserved as-is and never duplicated.
"""


def merge_missing_document_type_defaults(stored, canonical):
    """`stored` with any canonical "Label:CODE" entry appended if its label is not already
    present (compared by label, the part before ":"), preserving order and custom entries.
    """
    stored = list(stored or [])
    existing_labels = {str(item).split(":", 1)[0] for item in stored}
    merged = list(stored)
    for entry in canonical:
        label = str(entry).split(":", 1)[0]
        if label not in existing_labels:
            merged.append(entry)
            existing_labels.add(label)
    return merged
