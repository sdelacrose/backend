# Supplier portal V1

Disabled by default. Local activation was authorized and completed on 8 November 2026: only `supplier_portal.0001_initial` was applied to `links_local`. Recette and GitHub remain unchanged. The active local copy was merged while preserving other work.

## Contract

An authorized member of the project-owning company grants an existing, active LINKS user access to one supplier-origin document. No role, project membership or other document access is granted. Account provisioning and automatic invitation emails are not part of V1.

Suppliers use `/supplier-portal` with the existing LINKS authentication. The standalone layout does not load internal navigation, configuration, chat or role fetchers. Backend grants, not this layout, enforce access. Downloads use authenticated API requests, never storage URLs or public share tokens.

A supplier can upload the first file or a corrected revision for an assigned register entry. The existing document revision service preserves version numbering, duplicate detection, hashes, prior files, withdrawal and workflow guards. A submitted revision cannot be replaced while awaiting published feedback. Surveillance creation remains an explicit internal action.

Internal reviewers keep using Supplier Surveillance, distinct from internal document signatures. A closed surveillance can be published once, with a separate supplier-facing message. COMMENTED/REJECTED decisions require public instructions. Internal reviewer identities, responses, snapshots, closure comments and internal evidence PDFs are not included in supplier responses. The published result, project and supplier are frozen. Reassigning a document to another supplier/project does not expose its previous supplier's publications or submissions.

Revocation removes subsequent list, detail, upload and download access while preserving grants, submissions and published returns. Data already downloaded by the recipient cannot be recalled.

## Schema and activation

The independent Django app `supplier_portal` avoids introducing another leaf in the existing `project_management` migration graph. Its only initial migration is `supplier_portal.0001_initial`, depending on documentary migration `project_management.0234_transmittal_send_proof` and the configured user model. No unrelated migration is bundled.

Activation requires separate user approval. Before activation: back up the local database; integrate the candidate changes while preserving other local work; inspect the migration plan; apply only the portal migration if its dependencies are already applied; then set `SUPPLIER_PORTAL_ENABLED=True` in the local environment. Default: False. Upload limit: 50 MiB, configurable with `SUPPLIER_PORTAL_MAX_FILE_BYTES`.

In the active local checkout, `project_management/models/__init__.py`, `project_management/urls.py` and frontend `src/middleware.ts` contain other changes. Merge only the portal imports/routes/predicate; do not overwrite those files with release-checkout copies. Translation namespaces must also be merged individually. No activation, commit, push, recette deployment or migration execution is authorized by this README.

## API

All routes below are under `/api/project_management/`.

| Route | Methods | Scope |
| --- | --- | --- |
| `supplier-portal/documents` | GET | Current user's explicit grants |
| `supplier-portal/documents/{document}` | GET, POST multipart | Public detail / file submission |
| `supplier-portal/documents/{document}/revisions/{revision}/download` | GET | Shared revision, checked again on every request |
| `document/{document}/supplier-portal-access` | GET, POST, DELETE | Licensed internal document manager; account email / grant id |
| `document/{document}/supplier-surveillance/{surveillance}/publish` | GET, POST | Licensed internal document manager; publication status / public comment |

## Verification

- Backend candidate: 234 targeted tests PASS, including 26 portal tests and surveillance/history/evidence regression coverage. Isolated PostgreSQL database with schema synchronization; the application migration was not executed.
- Frontend: 120 portal/surveillance tests PASS; 52 internal workflow/revision tests PASS.
- TypeScript, targeted lint, production build and migration/model consistency check PASS.
- Real browser login, account provisioning, live upload and user acceptance remain to be verified after separately approved local activation. No external mail was sent.
