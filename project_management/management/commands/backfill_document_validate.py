"""GED-WF.1.1: controlled catch-up of the dedicated `documents.validate` right.

Before this right existed, any user who could see a document and was the assigned actor of a workflow
step could sign / verify / approve / reject. This command grants `documents.validate` ONLY to the
Company / Project role a user really holds in the scope of a document where they are (or were) an
assigned actor, when that role gives access to documents. It never grants by `documents.update` or by
`documents.view` alone, never to default (shared) roles, never to roles of another company, and skips
roles that already carry `documents.full_access` (honoured as the superior right).

    manage.py backfill_document_validate                       # dry run (default): lists, changes nothing
    manage.py backfill_document_validate --apply --backup FILE # grants, and writes the granted role ids
    manage.py backfill_document_validate --revert FILE         # removes exactly the grants of that file
"""
import json
import os
from collections import OrderedDict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

KEY = "documents.validate"
ACCESS_KEYS = ("documents.view", "documents.view_assigned_created", "documents.full_access")


class Command(BaseCommand):
    help = "Catch up documents.validate for existing workflow actors (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the grants (default: dry run).")
        parser.add_argument("--backup", help="JSON file receiving the granted role ids (required with --apply).")
        parser.add_argument("--revert", help="Remove the grants recorded in this backup file.")

    def handle(self, *args, **options):
        from models.models import Permission, RolePermission

        if options["revert"]:
            return self._revert(options["revert"], RolePermission)
        permission = Permission.objects.filter(key=KEY).first()
        if permission is None:
            raise CommandError("documents.validate does not exist: apply the migration first.")
        if options["apply"] and not options["backup"]:
            raise CommandError("--apply requires --backup FILE (needed to revert).")

        candidates, skipped = self._collect()
        self.stdout.write(f"{len(candidates)} role(s) to grant, {len(skipped)} actor/role case(s) NOT granted.")
        for role_id, info in candidates.items():
            self.stdout.write(f"  GRANT role {role_id} [{info['scope']}] {info['name']!r} <- users {sorted(info['users'])}")
        for line in skipped:
            self.stdout.write(f"  skip  {line}")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing written."))
            return
        granted = []
        with transaction.atomic():
            for role_id in candidates:
                _, created = RolePermission.objects.get_or_create(role_id=role_id, permission=permission)
                if created:
                    granted.append(role_id)
            # Written inside the transaction: if the backup cannot be written, nothing is granted. An existing
            # backup is merged, never overwritten, so a second run cannot lose the ids a revert needs.
            recorded = []
            if os.path.exists(options["backup"]):
                with open(options["backup"], encoding="utf-8") as handle:
                    recorded = json.load(handle).get("granted_role_ids", [])
            with open(options["backup"], "w", encoding="utf-8") as handle:
                json.dump({"permission": KEY, "granted_role_ids": sorted(set(recorded) | set(granted))}, handle)
        self.stdout.write(self.style.SUCCESS(f"Granted to {len(granted)} role(s): {granted}. Backup: {options['backup']}"))

    def _revert(self, path, role_permission_model):
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("permission") != KEY:
            raise CommandError("Backup file is not a documents.validate backfill.")
        deleted, _ = role_permission_model.objects.filter(
            permission__key=KEY, role_id__in=data.get("granted_role_ids", [])).delete()
        self.stdout.write(self.style.SUCCESS(f"Removed {deleted} grant(s)."))

    def _role_keys(self, role):
        from models.models import RolePermission
        return set(RolePermission.objects.filter(role=role).values_list("permission__key", flat=True))

    def _collect(self):
        from models.models import UserRole
        from project_management.models import DocumentWorkflow, ProjectTeamMembers

        candidates, skipped = OrderedDict(), []
        seen = set()
        workflows = DocumentWorkflow.objects.select_related(
            "revision__document__folder", "author", "verifier", "approver")
        for workflow in workflows:
            folder = workflow.revision.document.folder
            for user in {workflow.author, workflow.verifier, workflow.approver}:
                if user is None:
                    continue
                if folder.company_profile_id:
                    scope, scope_id = "company", folder.company_profile_id
                    if user.is_freelancer or user.company_id != scope_id:
                        key = (user.pk, scope, scope_id)
                        if key not in seen:
                            seen.add(key)
                            skipped.append(f"user {user.pk}: not a member of company {scope_id} (cross-company/freelancer never extended)")
                        continue
                    roles = [ur.role for ur in UserRole.objects.filter(
                        user=user, role__scope="company", role__type="solution", role__is_active=True,
                        role__company_id=scope_id).select_related("role")]
                elif folder.project_id:
                    scope, scope_id = "project", folder.project_id
                    member = ProjectTeamMembers.objects.filter(project_id=scope_id, team_member=user).select_related("project_role").first()
                    role = member.project_role if member else None
                    roles = [role] if role and role.is_active and role.scope == "project" and role.type == "solution" else []
                else:
                    continue
                key = (user.pk, scope, scope_id)
                if key in seen:
                    continue
                seen.add(key)
                if not roles:
                    skipped.append(f"user {user.pk} {scope} {scope_id}: no active role in this scope")
                    continue
                for role in roles:
                    keys = self._role_keys(role)
                    if KEY in keys:
                        continue
                    if "documents.full_access" in keys:
                        skipped.append(f"user {user.pk} role {role.pk}: already covered by documents.full_access")
                    elif role.is_default:
                        skipped.append(f"user {user.pk} role {role.pk} {role.name!r}: default shared role, needs a manual decision")
                    elif not (keys & set(ACCESS_KEYS)):
                        skipped.append(f"user {user.pk} role {role.pk} {role.name!r}: gives no access to documents")
                    else:
                        info = candidates.setdefault(role.pk, {"scope": scope, "name": role.name, "users": set()})
                        info["users"].add(user.pk)
        return candidates, skipped
