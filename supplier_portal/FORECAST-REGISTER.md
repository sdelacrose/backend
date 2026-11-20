# Forecast register increment

Candidate only. Migration `supplier_portal.0002_forecast_register` is generated, not applied. `SUPPLIER_REGISTER_ENABLED` defaults to False. The active local V1, recette and GitHub have not been changed by this increment.

## Lifecycle

An explicit SupplierProjectAccess connects an existing active account, supplier, project and destination folder. It allows the supplier's accepted documents within this project, including documents accepted before this increment. Legacy per-document grants remain supported and are not upgraded implicitly.

Manual proposals and XLSX/UTF-8 CSV imports create audited proposal rows, not files or fake revisions. The proposal is the pending placeholder. A native fileless Document is materialized only when an authorized company user accepts it. Supplier metadata cannot override project, supplier, destination, author, state or acceptance. The same DocumentSerializer used by the existing form validates metadata; no extra mandatory Supplier Ref, internal Author or revision designation is invented.

Invalid rows and all occurrences of a within-batch duplicate are refused automatically. Duplicate matching is scoped to supplier/project and also covers the existing register (including withdrawn records) and pending/accepted proposals. References use Unicode normalization, trimmed/collapsed whitespace and case folding. Without a supplier reference, title/type/discipline form the identity. Refused rows reserve no identity and can be corrected by submitting a new proposal; their original history is never overwritten.

Company decisions are serialized on the project row. Accept-all skips previously refused or accepted rows and revalidates pending rows against current project configuration and register contents. Selective acceptance unlocks individual entries immediately. Client refusal requires a reason; decisions and actors are retained. The client here is the project-owning company, not the future separate client-surveillance process.

The supplier chooses one project. Dashboard totals cover only accepted documents accessible in that project; pending proposals are separate. Received counts documents with files, not uploads. Surveillance totals and Commented/Reviewed/Rejected counts use the last revision's published return. Internal closed-but-unpublished results remain hidden. A corrected revision starts awaiting a new review. Percentages use accepted total as denominator, with zero-safe division.

Bulk physical upload matches each of at most 50 files to one accepted document in the selected project. Per-file validation is isolated: valid files are retained and failures are reported individually for correction/retry. Every file still passes the existing revision, withdrawal, workflow, duplicate-content and pending-review guards. Project access and file ownership are checked on the backend for every upload/download.

## Interfaces and bounds

- Supplier: `/supplier-portal`, project selector, paged document table, KPI, manual proposals, Excel/CSV imports, proposal history, single and bulk physical deposits.
- Company: `/supplier-register`, accessible from Project Documents; project-scoped supplier account grants, all-supplier batch list, select-valid/all-valid acceptance and reasoned refusal.
- API prefix: `/api/project_management/supplier-register/`; project/access/batch IDs are scoped server-side.
- Template: readable EN/FR/DE headers and instructions, project choices and Excel dropdowns. No physical upload, revision or internal IDs are required in the forecast list.
- Metadata: 1000 rows, 5 MiB compressed input / 25 MiB expanded workbook; formula cells are rejected. Bulk physical files: 50 files / 250 MiB total, plus existing per-file limit.
- Batches are paginated; row/document tables are paged. Dashboard and batch details refresh periodically. Dashboard query count is bounded rather than one SQL query per document.

## Activation requirements

Separate user approval is required before applying 0002 or modifying the active checkout. Integrate only this increment while preserving unrelated local changes, inspect the migration plan and keep a database backup. Backend code and 0002 must be activated together: the submission model gains a nullable project-access FK, so the feature flag is not a substitute for its schema. Do not serve the new model against the old schema. Apply only supplier_portal.0002_forecast_register, then enable SUPPLIER_REGISTER_ENABLED locally and verify V1 plus the new tests. No data migration or default project grant is included. Do not deploy recette, push GitHub, start workers, send mail or touch RH as part of local activation.

Automated evidence and the user checklist are in LIVRAISON-DOCUMENTAIRE/SUPPLIER-REGISTER-*.log and REGISTRE-FOURNISSEUR-ESSAIS-20261108.txt. Real browser/API acceptance of this increment remains pending approved activation; the earlier V1 validation is retained separately.
