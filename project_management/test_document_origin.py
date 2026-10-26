from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from models.models import User

from project_management.filters import DocumentFilter
from project_management.models import (
    ContactCompany,
    Document,
    DocumentOrigin,
    DocumentRevision,
    DocumentWorkflow,
)
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_workflow import (
    start_workflow,
    start_workflow_for_document,
)
from project_management.test_document_fileless import FilelessBase
from project_management.test_document_workflow import THREE, TWO
from project_management.views.document_workflow import DocumentWorkflowViewSet

INTERNAL = DocumentOrigin.INTERNAL
CLIENT = DocumentOrigin.CLIENT
SUPPLIER = DocumentOrigin.SUPPLIER


class OriginBase(FilelessBase):
    def setUp(self):
        super().setUp()
        self.client_company = self.network_company("Acme Client")
        self.supplier_company = self.network_company("Bolt Supplier")

    def network_company(self, name, **owner):
        # A Network company (ContactCompany) of the library owner (the project owner here).
        if not owner:
            owner = (
                {"company_profile": self.creator.company}
                if getattr(self.creator, "company_id", None)
                else {"user": self.creator}
            )
        return ContactCompany.objects.create(
            company_name=name, contact_email=f"{name.split()[0].lower()}@links-local.invalid", **owner
        )

    def payload(self, **extra):
        return {
            "folder": self.folder.id,
            "file_name": "Received plan",
            "document_type": "Drawing",
            "discipline": "Structure",
            **extra,
        }

    def build(self, data, instance=None, partial=False):
        return DocumentSerializer(
            instance,
            data=data,
            partial=partial,
            context={"user": self.creator, "include_control": True},
        )

    def create(self, **extra):
        serializer = self.build(self.payload(**extra))
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()

    def errors_of(self, data, instance=None, partial=False):
        serializer = self.build(data, instance, partial)
        with self.relaxed():
            self.assertFalse(serializer.is_valid())
        return serializer.errors

    def update(self, document, **data):
        serializer = self.build(data, document, partial=True)
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()


class DocumentOriginModelTests(OriginBase):
    def test_a_legacy_document_defaults_to_internal(self):
        legacy = self.legacy_document()
        legacy.refresh_from_db()
        self.assertEqual(legacy.document_origin, INTERNAL)
        self.assertIsNone(legacy.external_issuer_company)
        self.assertEqual(
            DocumentSerializer(legacy, context={"user": self.creator}).data[
                "document_origin"
            ],
            "INTERNAL",
        )

    def test_origin_is_never_inferred_from_other_metadata(self):
        document = self.create(
            transmittal_category="", status="Issued", document_type="Drawing"
        )
        # Nothing but the explicit value decides it.
        self.assertEqual(document.document_origin, INTERNAL)


class DocumentOriginCreationTests(OriginBase):
    def test_create_an_internal_document(self):
        document = self.create(document_origin="INTERNAL")
        self.assertEqual(document.document_origin, INTERNAL)
        self.assertIsNone(document.external_issuer_company)

    def test_create_without_origin_is_internal(self):
        self.assertEqual(self.create().document_origin, INTERNAL)

    def test_create_a_client_document_with_its_issuer(self):
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        document.refresh_from_db()
        self.assertEqual(document.document_origin, CLIENT)
        self.assertEqual(document.external_issuer_company, self.client_company)

    def test_create_a_supplier_document_with_its_issuer(self):
        document = self.create(
            document_origin="SUPPLIER", external_issuer_company=self.supplier_company.id
        )
        self.assertEqual(document.document_origin, SUPPLIER)
        self.assertEqual(document.external_issuer_company, self.supplier_company)

    def test_the_issuer_is_the_canonical_network_company(self):
        field = Document._meta.get_field("external_issuer_company")
        self.assertIs(field.related_model, ContactCompany)
        self.assertTrue(field.null)  # never mandatory at database level

    def test_the_serializer_exposes_the_issuer_as_id_and_name(self):
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        data = DocumentSerializer(document, context={"user": self.creator}).data
        self.assertEqual(data["document_origin"], "CLIENT")
        self.assertEqual(
            data["external_issuer_company"],
            {"id": self.client_company.id, "name": "Acme Client"},
        )
        internal = DocumentSerializer(self.create(), context={"user": self.creator}).data
        self.assertIsNone(internal["external_issuer_company"])

    def test_an_external_document_requires_its_issuer(self):
        for origin in ("CLIENT", "SUPPLIER"):
            errors = self.errors_of(self.payload(document_origin=origin))
            self.assertIn("external_issuer_company", errors, origin)

    def test_an_unknown_origin_is_rejected(self):
        self.assertIn("document_origin", self.errors_of(self.payload(document_origin="OTHER")))

    def test_an_issuer_sent_for_an_internal_document_is_cleaned_up(self):
        document = self.create(
            document_origin="INTERNAL", external_issuer_company=self.client_company.id
        )
        self.assertIsNone(document.external_issuer_company)

    def test_the_issuer_must_belong_to_the_library_network(self):
        stranger = ContactCompany.objects.create(
            company_name="Foreign Co", contact_email="f@links-local.invalid"
        )
        errors = self.errors_of(
            self.payload(document_origin="CLIENT", external_issuer_company=stranger.id)
        )
        self.assertIn("external_issuer_company", errors)

    def test_a_deleted_network_company_cannot_be_used(self):
        self.client_company.is_deleted = True
        self.client_company.save()
        errors = self.errors_of(
            self.payload(
                document_origin="CLIENT", external_issuer_company=self.client_company.id
            )
        )
        self.assertIn("external_issuer_company", errors)

    def test_a_fileless_external_document_is_still_to_create(self):
        document = self.create(
            document_origin="SUPPLIER", external_issuer_company=self.supplier_company.id
        )
        self.assertEqual(self.internal_status(document), "TO_CREATE")
        self.assertFalse(DocumentRevision.objects.filter(document=document).exists())

    def test_the_first_main_file_of_an_external_document_is_version_1(self):
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        data = self.upload_first_file(document)
        self.assertEqual(data["version_number"], 1)
        document.refresh_from_db()
        self.assertEqual(document.document_origin, CLIENT)
        self.assertEqual(self.internal_status(document), "DRAFT")


class DocumentOriginUpdateTests(OriginBase):
    def test_update_preserves_and_retrieves_the_origin(self):
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        self.update(document, description="Client brief")
        document.refresh_from_db()
        self.assertEqual(document.document_origin, CLIENT)
        self.assertEqual(document.external_issuer_company, self.client_company)

    def test_the_issuer_of_an_external_document_can_change(self):
        other = self.network_company("Other Client")
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        self.update(document, external_issuer_company=other.id)
        document.refresh_from_db()
        self.assertEqual(document.external_issuer_company, other)

    def test_an_external_document_cannot_lose_its_issuer(self):
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        errors = self.errors_of(
            {"external_issuer_company": None}, instance=document, partial=True
        )
        self.assertIn("external_issuer_company", errors)

    def test_reclassifying_without_workflow_history_is_allowed(self):
        document = self.create()
        self.update(
            document,
            document_origin="SUPPLIER",
            external_issuer_company=self.supplier_company.id,
        )
        document.refresh_from_db()
        self.assertEqual(document.document_origin, SUPPLIER)
        self.update(document, document_origin="INTERNAL")
        document.refresh_from_db()
        self.assertEqual(document.document_origin, INTERNAL)
        self.assertIsNone(document.external_issuer_company)

    def test_origin_is_immutable_once_an_internal_workflow_exists(self):
        document = self.create()
        self.upload_first_file(document)
        revision = DocumentRevision.objects.get(document=document)
        start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        errors = self.errors_of(
            {
                "document_origin": "CLIENT",
                "external_issuer_company": self.client_company.id,
            },
            instance=document,
            partial=True,
        )
        self.assertIn("document_origin", errors)
        document.refresh_from_db()
        self.assertEqual(document.document_origin, INTERNAL)

    def test_origin_stays_immutable_after_the_workflow_is_completed(self):
        document = self.create()
        self.upload_first_file(document)
        self.doc = document
        self.approve(DocumentRevision.objects.get(document=document))
        errors = self.errors_of(
            {"document_origin": "SUPPLIER", "external_issuer_company": self.supplier_company.id},
            instance=document,
            partial=True,
        )
        self.assertIn("document_origin", errors)

    def test_resending_the_same_origin_with_a_workflow_is_harmless(self):
        document = self.create()
        self.upload_first_file(document)
        revision = DocumentRevision.objects.get(document=document)
        start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        self.update(document, document_origin="INTERNAL", description="Still internal")
        document.refresh_from_db()
        self.assertEqual(document.description, "Still internal")


class DocumentOriginWorkflowGuardTests(OriginBase):
    def external(self, origin, company):
        document = self.create(document_origin=origin, external_issuer_company=company.id)
        self.upload_first_file(document)
        return document, DocumentRevision.objects.get(document=document)

    def test_an_internal_document_can_start_the_workflow(self):
        document = self.create()
        self.upload_first_file(document)
        revision = DocumentRevision.objects.get(document=document)
        workflow = start_workflow(revision, THREE, self.alice, self.bob, self.carol, self.creator)
        self.assertEqual(workflow.status, DocumentWorkflow.Status.DRAFT)

    def test_a_client_document_cannot_start_the_internal_workflow(self):
        document, revision = self.external(CLIENT, self.client_company)
        with self.assertRaises(DjangoValidationError) as ctx:
            start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        self.assertIn("internal documents only", str(ctx.exception))
        self.assertFalse(DocumentWorkflow.objects.exists())

    def test_a_supplier_document_cannot_start_the_internal_workflow(self):
        document, revision = self.external(SUPPLIER, self.supplier_company)
        with self.assertRaises(DjangoValidationError):
            start_workflow(revision, THREE, self.alice, self.bob, self.carol, self.creator)
        with self.assertRaises(DjangoValidationError):
            start_workflow_for_document(
                document, TWO, self.alice, None, self.carol, self.creator
            )
        self.assertFalse(DocumentWorkflow.objects.exists())

    def test_an_external_document_never_derives_a_validation_status(self):
        document, _ = self.external(CLIENT, self.client_company)
        self.assertEqual(self.internal_status(document), "DRAFT")

    def test_the_direct_api_launch_is_rejected_server_side(self):
        document, revision = self.external(SUPPLIER, self.supplier_company)
        view = DocumentWorkflowViewSet.as_view({"post": "start"}, permission_classes=[])
        request = APIRequestFactory().post(
            "/x/",
            {
                "workflow_type": "TWO_STEP",
                "author": self.alice.pk,
                "approver": self.carol.pk,
            },
            format="json",
        )
        force_authenticate(request, user=self.creator)
        from unittest.mock import patch

        from models.models import User

        with patch(
            "project_management.views.document_workflow.get_register_document_or_404",
            return_value=document,
        ), patch(
            "project_management.views.document_workflow.assert_can_update_document"
        ), patch(
            "project_management.views.document_workflow.get_allowed_owner_queryset",
            return_value=User.objects.all(),
        ):
            response = view(request, document_id=document.id, revision_id=revision.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("internal documents only", str(response.data))
        self.assertFalse(DocumentWorkflow.objects.exists())


class DocumentOriginFilterTests(OriginBase):
    def test_the_register_filter_selects_by_origin(self):
        internal = self.create()
        client = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        supplier = self.create(
            document_origin="SUPPLIER", external_issuer_company=self.supplier_company.id
        )

        def ids(value):
            return set(
                DocumentFilter(
                    {"document_origin": value}, queryset=Document.objects.all()
                ).qs.values_list("id", flat=True)
            )

        self.assertEqual(ids("CLIENT"), {client.id})
        self.assertEqual(ids("SUPPLIER"), {supplier.id})
        self.assertIn(internal.id, ids("INTERNAL"))
        self.assertFalse(ids("INTERNAL") & {client.id, supplier.id})
        self.assertEqual(ids("CLIENT,SUPPLIER"), {client.id, supplier.id})


class DocumentOriginLockTests(OriginBase):
    def locked(self, document, annotated=False):
        instance = document
        if annotated:
            from django.db.models import Exists, OuterRef

            instance = (
                Document.objects.filter(pk=document.pk)
                .annotate(
                    has_workflow_history=Exists(
                        DocumentWorkflow.objects.filter(
                            revision__document=OuterRef("pk")
                        )
                    )
                )
                .get()
            )
        return DocumentSerializer(
            instance, context={"user": self.creator, "include_control": True}
        ).data["origin_locked"]

    def test_the_origin_is_unlocked_until_a_workflow_exists(self):
        document = self.create()
        self.assertFalse(self.locked(document))
        self.upload_first_file(document)
        start_workflow(
            DocumentRevision.objects.get(document=document),
            TWO,
            self.alice,
            None,
            self.carol,
            self.creator,
        )
        self.assertTrue(self.locked(document))
        self.assertTrue(self.locked(document, annotated=True))


class DocumentIssuerLookupTests(OriginBase):
    """Restricted lookup of the external issuer company (id + company_name only)."""

    URL = "/api/project_management/document/issuer-companies"
    BROAD = "/api/project_management/contact-company/"

    def tenant(self, name):
        from models.models import CompanyProfile

        admin = User.objects.create_user(
            email=f"{name}.admin@links-local.invalid",
            first_name=name,
            last_name="Admin",
            title="Mr",
            user_type="company_user",
        )
        return CompanyProfile.objects.create(
            admin_user=admin,
            company_name=f"{name} Tenant",
            company_address="1 Test Street",
            post_code="00000",
            company_town="Testtown",
            company_country="Testland",
            company_tell_no="000",
            company_mob_no="001",
            company_web_address="http://localhost",
            company_email_address=f"{name}@links-local.invalid",
            company_tax_reference="TAX",
            company_iban="IBAN",
        )

    def member(self, name, company, keys):
        """A company user whose role holds exactly `keys` (never contact_management.view)."""
        from models.models import Permission, Role, RolePermission, UserRole

        user = User.objects.create_user(
            email=f"{name}@links-local.invalid",
            first_name=name,
            last_name="Member",
            title="Mr",
            user_type="company_user",
        )
        user.company = company
        user.save()
        role = Role.objects.create(
            name=f"{name} role",
            company=company,
            type="solution",
            scope="company",
            is_active=True,
            created_by=user,
        )
        for key in keys:
            permission, _ = Permission.objects.get_or_create(
                key=key, defaults={"name": key, "module": key.split(".")[0]}
            )
            RolePermission.objects.get_or_create(role=role, permission=permission)
        UserRole.objects.create(user=user, role=role, company=company, assigned_by=user)
        return user

    def get(self, user, **params):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(self.URL, params)

    def setUp(self):
        super().setUp()
        self.company = self.tenant("alpha")
        self.other_company = self.tenant("beta")
        self.mine = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Acme Client",
            contact_email="a@links-local.invalid",
            iban="SECRET-IBAN",
            bic="SECRET-BIC",
            company_address="Secret street",
            contact_name="Secret Contact",
        )
        self.gone = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Deleted Co",
            contact_email="d@links-local.invalid",
            is_deleted=True,
        )
        self.foreign = ContactCompany.objects.create(
            company_profile=self.other_company,
            company_name="Foreign Co",
            contact_email="f@links-local.invalid",
        )

    def test_a_documents_create_user_can_use_the_lookup_without_network_permission(self):
        user = self.member("creator", self.company, ["documents.create"])
        response = self.get(user)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data], [self.mine.id])

    def test_a_documents_update_user_can_use_the_lookup_to_edit_the_issuer(self):
        user = self.member("editor", self.company, ["documents.update"])
        self.assertEqual(self.get(user).status_code, 200)

    def test_documents_view_alone_does_not_open_the_lookup(self):
        user = self.member("reader", self.company, ["documents.view"])
        self.assertEqual(self.get(user).status_code, 403)

    def test_a_user_without_any_role_or_login_is_refused(self):
        user = User.objects.create_user(
            email="norole@links-local.invalid",
            first_name="No",
            last_name="Role",
            title="Mr",
            user_type="company_user",
        )
        self.assertEqual(self.get(user).status_code, 403)
        self.assertIn(APIClient().get(self.URL).status_code, (401, 403))

    def test_the_response_contains_only_id_and_company_name(self):
        user = self.member("creator", self.company, ["documents.create"])
        rows = self.get(user).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"id", "company_name"})
        self.assertNotIn("SECRET", str(rows).upper().replace("SECRET STREET", ""))
        self.assertNotIn("Secret Contact", str(rows))

    def test_only_live_companies_of_the_own_tenant_are_returned(self):
        user = self.member("creator", self.company, ["documents.create"])
        names = [row["company_name"] for row in self.get(user).data]
        self.assertIn("Acme Client", names)
        self.assertNotIn("Foreign Co", names)  # another tenant
        self.assertNotIn("Deleted Co", names)  # deleted
        other = self.member("other", self.other_company, ["documents.create"])
        self.assertEqual(
            [row["company_name"] for row in self.get(other).data], ["Foreign Co"]
        )

    def test_a_user_without_a_company_never_gets_ownerless_companies(self):
        orphan = ContactCompany.objects.create(
            company_name="Orphan Co", contact_email="o@links-local.invalid"
        )
        user = self.member("creator", self.company, ["documents.create"])
        User.objects.filter(pk=user.pk).update(company=None)
        user.refresh_from_db()
        response = self.get(user)
        data = response.data if response.status_code == 200 else []
        self.assertNotIn(orphan.id, [row["id"] for row in data])

    def test_the_optional_search_filters_by_company_name_only(self):
        ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Bolt Supplier",
            contact_email="acme@links-local.invalid",
        )
        user = self.member("creator", self.company, ["documents.create"])
        rows = self.get(user, search="acme").data
        self.assertEqual([row["company_name"] for row in rows], ["Acme Client"])

    def test_the_lookup_does_not_open_the_broad_network_endpoint(self):
        user = self.member(
            "creator", self.company, ["documents.create", "documents.update"]
        )
        self.assertEqual(self.get(user).status_code, 200)
        client = APIClient()
        client.force_authenticate(user=user)
        self.assertEqual(client.get(self.BROAD).status_code, 403)

    def test_the_lookup_has_no_contact_management_dependency(self):
        from models.models import RolePermission

        user = self.member("creator", self.company, ["documents.create"])
        keys = set(
            RolePermission.objects.filter(
                role__user_company_roles__user=user
            ).values_list("permission__key", flat=True)
        )
        self.assertEqual(keys, {"documents.create"})
        self.assertEqual(self.get(user).status_code, 200)

    def test_a_freelancer_only_sees_the_own_live_companies(self):
        from models.models import Permission, Role, RolePermission, UserRole

        freelancer = User.objects.create_user(
            email="free@links-local.invalid",
            first_name="Free",
            last_name="Lancer",
            title="Mr",
            user_type="freelancer",
        )
        role = Role.objects.create(
            name="free role",
            freelancer=freelancer,
            type="solution",
            scope="company",
            is_active=True,
            created_by=freelancer,
        )
        permission, _ = Permission.objects.get_or_create(
            key="documents.create", defaults={"name": "c", "module": "documents"}
        )
        RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=freelancer, role=role, freelancer=freelancer)
        own = ContactCompany.objects.create(
            user=freelancer, company_name="Free Client", contact_email="fc@links-local.invalid"
        )
        response = self.get(freelancer)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data], [own.id])

    def test_the_serializer_still_rejects_another_tenants_issuer(self):
        # The lookup never replaces validation: a hand-sent foreign id is refused.
        errors = self.errors_of(
            self.payload(
                document_origin="CLIENT", external_issuer_company=self.foreign.id
            )
        )
        self.assertIn("external_issuer_company", errors)

    # ---- inline creation from the Document form: canonical ContactCompany, no new access ----

    CREATE = "/api/project_management/contact-company/"

    def create_company(self, user, name="Inline Client Co"):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.post(
            self.CREATE,
            {"company_name": name, "contact_email": "inline@links-local.invalid"},
            format="json",
        )

    def test_a_company_created_with_the_network_permission_appears_in_the_lookup(self):
        user = self.member(
            "netcreator", self.company, ["documents.create", "contact_management.create"]
        )
        response = self.create_company(user)
        self.assertEqual(response.status_code, 201, response.data)
        created = ContactCompany.objects.get(pk=response.data["id"])
        # the canonical Network entity, owned by the creator's tenant (no duplicate table)
        self.assertEqual(created.company_profile_id, self.company.id)
        rows = self.get(user).data
        self.assertIn({"id": created.id, "company_name": "Inline Client Co"}, rows)
        for row in rows:
            self.assertEqual(set(row), {"id", "company_name"})

    def test_the_created_company_is_a_valid_issuer_of_the_tenants_library(self):
        from project_management.models import Folder

        user = self.member(
            "netcreator", self.company, ["documents.create", "contact_management.create"]
        )
        created = ContactCompany.objects.get(pk=self.create_company(user).data["id"])
        folder = Folder.objects.create(company_profile=self.company, name="Alpha docs")
        other_folder = Folder.objects.create(
            company_profile=self.other_company, name="Beta docs"
        )
        self.assertTrue(DocumentSerializer._issuer_in_scope(created, folder))
        self.assertFalse(DocumentSerializer._issuer_in_scope(created, other_folder))

    def test_a_documents_only_user_cannot_create_companies_or_read_the_network(self):
        user = self.member(
            "docsonly", self.company, ["documents.create", "documents.update"]
        )
        self.assertEqual(self.create_company(user).status_code, 403)
        self.assertFalse(
            ContactCompany.objects.filter(company_name="Inline Client Co").exists()
        )
        client = APIClient()
        client.force_authenticate(user=user)
        self.assertEqual(client.get(self.BROAD).status_code, 403)
        self.assertEqual(self.get(user).status_code, 200)  # the lookup still works


class DocumentOriginCompanyScopeTests(OriginBase):
    """LOT 3D-3D1 pre-flight A: a Company Document's origin is always INTERNAL, immutably --
    previously only enforced by the frontend (it never sends anything else for a Company
    Document); this proves the backend now rejects a bypass too, the same way an origin change
    once a workflow exists is already rejected (_validate_origin)."""

    def setUp(self):
        super().setUp()
        from project_management.models import Folder

        # OriginBase/FilelessBase's fixture is project-scoped (self.project/self.folder) and
        # has no separate self.company -- reuses that same project's own company_profile for a
        # genuinely Company-scoped (no project) folder here instead of inventing a second one.
        self.company_folder = Folder.objects.create(
            company_profile=self.project.company_profile, name="Company Origin Test Folder",
        )

    def test_a_company_document_defaults_to_internal(self):
        # discipline="" here: this Company-scoped folder has no project ECPMPConfig, so the
        # default payload's "Structure" discipline has no configured reference code to resolve
        # (unrelated to this test's own concern -- origin, not discipline/reference).
        document = self.create(folder=self.company_folder.id, discipline="")
        self.assertEqual(document.document_origin, INTERNAL)

    def test_a_company_document_explicitly_sent_as_internal_is_accepted(self):
        document = self.create(folder=self.company_folder.id, document_origin="INTERNAL", discipline="")
        self.assertEqual(document.document_origin, INTERNAL)

    def test_a_company_document_cannot_be_created_as_client(self):
        errors = self.errors_of(self.payload(folder=self.company_folder.id, document_origin="CLIENT"))
        self.assertIn("document_origin", errors)

    def test_a_company_document_cannot_be_created_as_supplier(self):
        errors = self.errors_of(self.payload(folder=self.company_folder.id, document_origin="SUPPLIER"))
        self.assertIn("document_origin", errors)

    def test_a_company_document_cannot_be_reclassified_by_a_direct_update_either(self):
        document = self.create(folder=self.company_folder.id, discipline="")
        errors = self.errors_of({"document_origin": "CLIENT"}, instance=document, partial=True)
        self.assertIn("document_origin", errors)

    def test_a_project_document_is_unaffected_by_this_rule(self):
        # self.folder (OriginBase/FilelessBase) is project-scoped -- CLIENT/SUPPLIER still work.
        document = self.create(document_origin="CLIENT", external_issuer_company=self.client_company.id)
        self.assertEqual(document.document_origin, CLIENT)


class L2D1AuthorRequirementTests(OriginBase):
    """L2D-1 section 13/17: Author is required for an INTERNAL creation -- but never
    retroactively, never for SUPPLIER/CLIENT, and the pre-existing "default author to the
    current user" convenience (DocumentSerializer.create) already satisfies it, so this never
    regresses a caller that simply omits `author` and relies on that fallback."""

    def test_internal_creation_without_author_and_without_a_fallback_user_is_rejected(self):
        # No `author` in the payload AND no context user at all: nothing for create() to fall
        # back to, so this is the one case the validate()-level check must actually catch.
        serializer = DocumentSerializer(data=self.payload(), context={})
        with self.relaxed():
            self.assertFalse(serializer.is_valid())
        self.assertIn("author", serializer.errors)

    def test_internal_creation_without_explicit_author_falls_back_to_the_context_user(self):
        # The real create endpoint always injects context['user'] (DocumentViewSet.get_serializer)
        # -- this is the realistic "Author omitted" case, and must still succeed.
        document = self.create()
        self.assertEqual(document.author_id, self.creator.id)

    def test_internal_creation_with_an_explicit_author_succeeds(self):
        document = self.create(author=self.creator.id)
        self.assertEqual(document.author_id, self.creator.id)

    def test_supplier_creation_does_not_require_author(self):
        # self.create() already asserts is_valid() is True -- the real assertion here is simply
        # that omitting `author` never raises a validation error for SUPPLIER (unlike INTERNAL
        # with no context user). The pre-existing "default author to the current user" fallback
        # (create(), unconditional on origin) still applies regardless -- that is unrelated,
        # unchanged behaviour, not the "required" rule this lot adds.
        document = self.create(
            document_origin="SUPPLIER", external_issuer_company=self.supplier_company.id
        )
        self.assertEqual(document.document_origin, SUPPLIER)

    def test_client_creation_does_not_require_author(self):
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        self.assertEqual(document.document_origin, CLIENT)

    def test_updating_an_existing_document_never_requires_author_even_when_null(self):
        # Never retroactive: an existing Document with author=NULL must keep saving fine.
        document = self.create()
        document.author = None
        document.save(update_fields=["author"])
        updated = self.update(document, description="updated")
        self.assertEqual(updated.description, "updated")


class L2D1PreferredVerifierAndApproverTests(OriginBase):
    """L2D-1 section 4/5/17: DocumentControl.verifier persists through the same mechanism as the
    existing DocumentControl.approver -- both are preselection defaults only, never the
    authoritative DocumentWorkflow actors (untouched by this lot)."""

    def test_preferred_verifier_can_be_persisted_on_create(self):
        document = self.create(verifier=self.creator.id)
        document.refresh_from_db()
        self.assertEqual(document.control.verifier_id, self.creator.id)

    def test_preferred_verifier_can_be_persisted_on_update(self):
        document = self.create()
        self.update(document, verifier=self.creator.id)
        document.refresh_from_db()
        self.assertEqual(document.control.verifier_id, self.creator.id)

    def test_existing_preferred_approver_behavior_is_unchanged(self):
        document = self.create(approver=self.creator.id)
        document.refresh_from_db()
        self.assertEqual(document.control.approver_id, self.creator.id)

    def test_verifier_and_approver_persist_and_clear_independently(self):
        document = self.create(verifier=self.creator.id, approver=self.creator.id)
        self.update(document, verifier="")
        document.refresh_from_db()
        self.assertIsNone(document.control.verifier_id)
        self.assertEqual(document.control.approver_id, self.creator.id)

    def test_serializer_exposes_both_under_include_control(self):
        document = self.create(verifier=self.creator.id, approver=self.creator.id)
        read = DocumentSerializer(
            Document.objects.get(pk=document.pk),
            context={"user": self.creator, "include_control": True},
        ).data
        self.assertEqual(read["verifier"]["id"], self.creator.id)
        self.assertEqual(read["approver"]["id"], self.creator.id)
