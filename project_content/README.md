# Project Content Generation

This app owns AI-assisted project Offers, Requirements Specifications, and FAT/CAT acceptance checklists. Links remains the source of truth: AI returns grounded structured proposals, users review them, and the backend performs all writes and document rendering.

## Runtime

Enable the feature with:

```text
PROJECT_DOCUMENT_GENERATION_ENABLED=True
PROJECT_DOCUMENT_AI_ENABLED=True
ACCEPTANCE_CHECKLISTS_ENABLED=True
PROJECT_CONTENT_QUEUE=project_document_generation
PROJECT_CONTENT_LIBREOFFICE_BINARY=soffice
```

The dedicated worker must consume only `project_document_generation`. It uses `Dockerfile.document-worker`, which installs a pinned LibreOffice package and runs with concurrency one to isolate office conversion memory.

## Deployment Order

1. Deploy shared permission registry migration `models.0114_project_content_permissions`.
2. Apply core migrations, including `project_management.0168` and all `project_content` migrations.
3. Deploy the document worker and verify `soffice --version` plus a DOCX-to-PDF smoke conversion.
4. Deploy the specification-extraction service with the project-content endpoints.
5. Deploy the core API, then the frontend.
6. Preview existing-project folders:

   ```bash
   python manage.py backfill_project_content_folders
   ```

7. Apply the reviewed folder changes:

   ```bash
   python manage.py backfill_project_content_folders --apply
   ```

8. Enable the three feature flags for the staging test tenant.

## Local Verification

Use a non-production project ID:

```bash
python manage.py seed_project_content_demo --project-id <id>
python manage.py seed_project_content_demo --project-id <id> --apply
python manage.py seed_project_content_demo --project-id <id> --apply
python manage.py verify_project_content_demo --project-id <id>
python manage.py test project_content
```

Running the seed twice is intentional and verifies idempotency. The fixture includes multilingual templates, mixed document and structured sources, linked opportunity data, paired FAT/CAT criteria, manual edits, conflicting inputs, and security test content.

For the 500-requirement and 1,000-criterion-per-phase load fixture:

```bash
python manage.py seed_project_content_demo --project-id <id> --apply --include-performance
python manage.py verify_project_content_demo --project-id <id> --minimum-requirements 500 --minimum-criteria 1000
```

## Security and Recovery

- Company templates are immutable versions. Only their display name can be changed; replacing a file creates a new version.
- Templates must be macro-free DOCX archives containing `{{SHAREMUNDO_CONTENT}}`. External relationships, encryption, unsafe paths, symlinks, malformed XML, and configured size limits are rejected.
- Generated DOCX and PDF revisions are immutable and retain SHA-256 checksums.
- Applying a reviewed draft fails if any selected source or approved generated-document input changed.
- Later acceptance runs are suggestion-only and preserve every manually edited field.
- System folders are identified by `system_key` and cannot be renamed, moved, repurposed, or deleted.
- Disable the three feature flags to roll back UI and generation access. Existing revisions, files, checklists, and audit history remain available in storage and the database.
