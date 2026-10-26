"""L3B/L3C: Supplier Surveillance foundation + Template library -- targeted tests only. No
reviewers/events/PDF/closure: those remain later lots, deliberately not covered here.
"""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile
from project_management.models import (
    DocumentRevision,
    SupplierSurveillance,
    SupplierSurveillanceTemplate,
    SupplierSurveillanceTemplateItem,
    SupplierSurveillanceTemplateSection,
)
from project_management.services.supplier_surveillance import (
    create_supplier_surveillance,
    generate_supplier_surveillance_reference,
    get_supplier_surveillance_for_revision,
)
from project_management.services.supplier_surveillance_template import (
    assert_template_usable_for_surveillance,
    build_template_snapshot,
    resolve_company_for_project,
)
from project_management.test_document_origin import CLIENT, INTERNAL, SUPPLIER, OriginBase
from project_management.views.supplier_surveillance import SupplierSurveillanceViewSet
from project_management.views.supplier_surveillance_template import (
    SupplierSurveillanceTemplateViewSet,
)


class SupplierSurveillanceBase(OriginBase):
    def make_supplier_revision(self, category="FOR_REVIEW", external_revision="A", **extra):
        document = self.create(
            document_origin=SUPPLIER,
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category=category,
            identification_number="VEN-4578-DWG-002",
            **extra,
        )
        self.upload_first_file(document, external_revision=external_revision)
        return DocumentRevision.objects.get(document=document)

    def make_internal_revision(self):
        document = self.create(document_origin=INTERNAL)
        self.upload_first_file(document)
        return DocumentRevision.objects.get(document=document)

    def make_client_revision(self):
        document = self.create(
            document_origin=CLIENT, external_issuer_company=self.client_company.id,
        )
        self.upload_first_file(document)
        return DocumentRevision.objects.get(document=document)

    def make_company(self):
        return CompanyProfile.objects.get(pk=resolve_company_for_project(self.project))

    def make_template(self, *, company=None, is_active=True, with_structure=True, name="General Supplier Review"):
        template = SupplierSurveillanceTemplate.objects.create(
            company=company or self.make_company(),
            name=name,
            is_active=is_active,
            created_by=self.creator,
        )
        if with_structure:
            section = SupplierSurveillanceTemplateSection.objects.create(
                template=template, title="Document Identification", order=1,
            )
            SupplierSurveillanceTemplateItem.objects.create(
                section=section,
                label="Document reference correct",
                item_type=SupplierSurveillanceTemplateItem.ItemType.CHECK,
                required=True,
                order=1,
            )
        return template


class SupplierSurveillanceTemplateModelTests(SupplierSurveillanceBase):
    # 1/2/3/4 -- Template model itself.
    def test_create_template(self):
        template = self.make_template()
        self.assertIsNotNone(template.pk)

    def test_template_company_ownership(self):
        company = self.make_company()
        template = self.make_template(company=company)
        self.assertEqual(template.company_id, company.id)

    def test_default_version_is_one(self):
        template = self.make_template()
        self.assertEqual(template.version, 1)

    def test_default_active_is_true(self):
        template = self.make_template()
        self.assertTrue(template.is_active)

    # 5/6 -- Section.
    def test_create_section(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(
            template=template, title="Section A", order=1,
        )
        self.assertEqual(section.template_id, template.id)

    def test_section_ordering(self):
        template = self.make_template(with_structure=False)
        second = SupplierSurveillanceTemplateSection.objects.create(
            template=template, title="Second", order=2,
        )
        first = SupplierSurveillanceTemplateSection.objects.create(
            template=template, title="First", order=1,
        )
        self.assertEqual(list(template.sections.all()), [first, second])

    # 7/8 -- Item.
    def test_create_item(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        item = SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="Item X", item_type="CHECK", order=1,
        )
        self.assertEqual(item.section_id, section.id)

    def test_item_ordering(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        second = SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="Second", item_type="CHECK", order=2,
        )
        first = SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="First", item_type="CHECK", order=1,
        )
        self.assertEqual(list(section.items.all()), [first, second])

    # 9/10/11/12 -- Item type vocabulary.
    def test_check_type_accepted(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        item = SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="A", item_type="CHECK", order=1,
        )
        self.assertEqual(item.item_type, "CHECK")

    def test_text_type_accepted(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        item = SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="A", item_type="TEXT", order=1,
        )
        self.assertEqual(item.item_type, "TEXT")

    def test_textarea_type_accepted(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        item = SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="A", item_type="TEXTAREA", order=1,
        )
        self.assertEqual(item.item_type, "TEXTAREA")

    def test_invalid_item_type_rejected_via_full_clean(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        item = SupplierSurveillanceTemplateItem(
            section=section, label="A", item_type="SIGNATURE", order=1,
        )
        with self.assertRaises(ValidationError):
            item.full_clean()


class TemplateUsableForSurveillanceTests(SupplierSurveillanceBase):
    # 13/14/15 -- structure/active/company validation at Surveillance-creation time.
    def test_template_without_usable_structure_rejected(self):
        template = self.make_template(with_structure=False)
        with self.assertRaises(ValidationError):
            assert_template_usable_for_surveillance(
                template, company_id=template.company_id
            )

    def test_inactive_template_rejected(self):
        template = self.make_template(is_active=False)
        with self.assertRaises(ValidationError):
            assert_template_usable_for_surveillance(
                template, company_id=template.company_id
            )

    def test_wrong_company_template_rejected(self):
        from models.models import User

        other_admin = User.objects.create_user(
            email="other.company.admin@links-local.invalid",
            first_name="Other",
            last_name="Admin",
            title="Mr",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            admin_user=other_admin, company_name="Other Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="o@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        template = self.make_template(company=other_company)
        own_company = self.make_company()
        with self.assertRaises(ValidationError):
            assert_template_usable_for_surveillance(template, company_id=own_company.id)


class CreateSupplierSurveillanceTests(SupplierSurveillanceBase):
    # 1/2/3/5/14/15/16/17/18 (L3B) -- success path and the initial, deliberately-empty schema.
    def test_create_for_supplier_for_review_revision_succeeds(self):
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template()
        )
        self.assertIsNotNone(surveillance.pk)

    def test_belongs_to_the_exact_revision(self):
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template()
        )
        self.assertEqual(surveillance.document_revision_id, revision.id)

    def test_project_is_derived_from_the_revision_not_accepted_as_input(self):
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template()
        )
        self.assertEqual(surveillance.project_id, revision.document.folder.project_id)

    def test_supplier_is_derived_from_the_document_not_duplicated(self):
        revision = self.make_supplier_revision()
        create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template()
        )
        # No supplier/issuer field exists on the model itself (L3A/L3B section 3): it is only
        # ever read via document_revision.document.external_issuer_company.
        field_names = {f.name for f in SupplierSurveillance._meta.get_fields()}
        self.assertNotIn("external_issuer_company", field_names)
        self.assertNotIn("supplier", field_names)
        self.assertEqual(
            revision.document.external_issuer_company_id, self.supplier_company.id
        )

    def test_reference_is_generated_and_unique(self):
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template()
        )
        self.assertTrue(surveillance.reference)
        self.assertTrue(surveillance.reference.startswith("SR-"))

    def test_reference_counter_increments_across_surveillances(self):
        first = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(),
            actor=self.creator,
            template=self.make_template(),
        )
        second = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(),
            actor=self.creator,
            template=self.make_template(),
        )
        self.assertNotEqual(first.reference, second.reference)

    def test_duplicate_surveillance_for_the_same_revision_is_rejected(self):
        # 24 (L3C) -- duplicate protection still works with Templates in the mix.
        revision = self.make_supplier_revision()
        template = self.make_template()
        create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template
        )
        with self.assertRaises(ValidationError):
            create_supplier_surveillance(
                document_revision=revision, actor=self.creator, template=template
            )
        self.assertEqual(
            SupplierSurveillance.objects.filter(document_revision=revision).count(), 1
        )

    def test_internal_revision_is_rejected(self):
        # 25 (L3C)
        revision = self.make_internal_revision()
        with self.assertRaises(ValidationError):
            create_supplier_surveillance(
                document_revision=revision, actor=self.creator, template=self.make_template()
            )

    def test_client_revision_is_rejected(self):
        # 26 (L3C)
        revision = self.make_client_revision()
        with self.assertRaises(ValidationError):
            create_supplier_surveillance(
                document_revision=revision, actor=self.creator, template=self.make_template()
            )

    def test_for_information_backend_creation_remains_possible_with_a_template(self):
        # 27 (L3C): FOR_INFORMATION must not be structurally blocked server-side when a valid
        # Template is explicitly provided -- only the frontend's normal entry point is
        # FOR_REVIEW-only.
        revision = self.make_supplier_revision(category="FOR_INFORMATION")
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template()
        )
        self.assertIsNotNone(surveillance.pk)

    def test_initial_status_is_draft(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(),
            actor=self.creator,
            template=self.make_template(),
        )
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.DRAFT)

    def test_result_and_new_revision_required_start_null(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(),
            actor=self.creator,
            template=self.make_template(),
        )
        self.assertIsNone(surveillance.result)
        self.assertIsNone(surveillance.new_revision_required)

    def test_created_by_is_the_acting_user(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(),
            actor=self.creator,
            template=self.make_template(),
        )
        self.assertEqual(surveillance.created_by_id, self.creator.id)

    def test_closed_at_stays_null(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(),
            actor=self.creator,
            template=self.make_template(),
        )
        self.assertIsNone(surveillance.closed_at)

    def test_without_a_template_is_rejected(self):
        with self.assertRaises(TypeError):
            create_supplier_surveillance(
                document_revision=self.make_supplier_revision(), actor=self.creator
            )


class SupplierSurveillanceSnapshotTests(SupplierSurveillanceBase):
    # 16/17/18/19/20/21/22/23 -- the most important L3C acceptance area.
    def test_snapshot_is_captured_at_creation(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(),
            actor=self.creator,
            template=self.make_template(),
        )
        self.assertIsNotNone(surveillance.template_snapshot)

    def test_snapshot_includes_template_identity(self):
        template = self.make_template(name="General Supplier Review")
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator, template=template
        )
        snapshot = surveillance.template_snapshot
        self.assertEqual(snapshot["template_id"], template.id)
        self.assertEqual(snapshot["template_name"], "General Supplier Review")
        self.assertEqual(snapshot["template_version"], 1)
        self.assertIn("captured_at", snapshot)

    def test_snapshot_includes_ordered_sections(self):
        template = self.make_template(with_structure=False)
        SupplierSurveillanceTemplateSection.objects.create(template=template, title="Second", order=2)
        SupplierSurveillanceTemplateSection.objects.create(template=template, title="First", order=1)
        snapshot = build_template_snapshot(template)
        self.assertEqual([s["title"] for s in snapshot["sections"]], ["First", "Second"])

    def test_snapshot_includes_ordered_items(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="Second", item_type="CHECK", order=2
        )
        SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="First", item_type="CHECK", order=1
        )
        snapshot = build_template_snapshot(template)
        self.assertEqual(
            [item["label"] for item in snapshot["sections"][0]["items"]], ["First", "Second"],
        )

    def test_snapshot_preserves_item_type_and_required(self):
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="A", item_type="TEXTAREA", required=False, order=1
        )
        snapshot = build_template_snapshot(template)
        item = snapshot["sections"][0]["items"][0]
        self.assertEqual(item["type"], "TEXTAREA")
        self.assertEqual(item["required"], False)

    def test_live_template_edit_does_not_alter_an_existing_snapshot(self):
        # The single most important L3C acceptance test (section 27).
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(
            template=template, title="Document Identification", order=1,
        )
        item = SupplierSurveillanceTemplateItem.objects.create(
            section=section,
            label="Document reference correct",
            item_type="CHECK",
            required=True,
            order=1,
        )
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator, template=template
        )
        original_snapshot = surveillance.template_snapshot

        template.name = "Renamed Template"
        template.save(update_fields=["name"])
        section.title = "Renamed Section"
        section.save(update_fields=["title"])
        item.label = "Document reference verified"
        item.required = False
        item.order = 2
        item.save(update_fields=["label", "required", "order"])

        surveillance.refresh_from_db()
        self.assertEqual(surveillance.template_snapshot, original_snapshot)
        self.assertEqual(surveillance.template_snapshot["template_name"], "General Supplier Review")
        self.assertEqual(
            surveillance.template_snapshot["sections"][0]["title"], "Document Identification",
        )
        self.assertEqual(
            surveillance.template_snapshot["sections"][0]["items"][0]["label"],
            "Document reference correct",
        )
        self.assertTrue(surveillance.template_snapshot["sections"][0]["items"][0]["required"])

        # A NEW Surveillance created afterwards picks up the renamed structure -- proving the
        # earlier snapshot's immutability was real, not just an untouched live Template.
        another = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator, template=template
        )
        self.assertEqual(another.template_snapshot["template_name"], "Renamed Template")
        self.assertEqual(
            another.template_snapshot["sections"][0]["items"][0]["label"],
            "Document reference verified",
        )

    def test_existing_l3b_null_snapshot_remains_readable(self):
        # L3B rows created before L3C (template_snapshot never set) must stay readable.
        revision = self.make_supplier_revision()
        surveillance = SupplierSurveillance.objects.create(
            project=revision.document.folder.project,
            document_revision=revision,
            reference="SR-LEGACY-00001",
            created_by=self.creator,
        )
        self.assertIsNone(surveillance.template_snapshot)
        fetched = get_supplier_surveillance_for_revision(revision)
        self.assertEqual(fetched.pk, surveillance.pk)
        self.assertIsNone(fetched.template_snapshot)


class SupplierSurveillanceReferenceTests(SupplierSurveillanceBase):
    # 6/7/21 (L3B) -- reference allocation in isolation.
    def test_reference_matches_the_transmittal_style_project_scoped_format(self):
        revision = self.make_supplier_revision()
        reference = generate_supplier_surveillance_reference(project=revision.document.folder.project)
        self.assertRegex(reference, r"^SR-[A-Z0-9]+-\d{5}$")

    def test_sequence_is_isolated_per_project(self):
        revision_a = self.make_supplier_revision()
        project_a = revision_a.document.folder.project
        other_project, _other_folder = self.make_project("Second Project")
        first = generate_supplier_surveillance_reference(project=project_a)
        second = generate_supplier_surveillance_reference(project=other_project)
        # Both start their own counter at 00001 -- independent per-project sequences.
        self.assertTrue(first.endswith("00001"))
        self.assertTrue(second.endswith("00001"))

    def test_concurrency_pattern_locks_the_counter_row(self):
        # Not an actual multi-thread test (expensive integration machinery, per L3B section
        # 32/21): confirms the allocator goes through select_for_update inside a real atomic
        # transaction by construction, mirroring generate_transmittal_number exactly.
        import inspect

        import project_management.services.supplier_surveillance as module

        source = inspect.getsource(module)
        self.assertIn(
            "@transaction.atomic\ndef generate_supplier_surveillance_reference",
            source,
        )
        self.assertIn(
            "SupplierSurveillanceReferenceCounter.objects.select_for_update()",
            source,
        )


class SupplierSurveillanceTemplateSerializerTests(SupplierSurveillanceBase):
    # The nested create/update sync (L3C section 17): the smallest API surface that still lets
    # the editor add/edit/remove Sections/Items freely, without a separate endpoint per row.
    def _serializer(self, instance=None, data=None, partial=False):
        from project_management.serializers.supplier_surveillance_template import (
            SupplierSurveillanceTemplateSerializer,
        )

        context = {"company": self.make_company(), "user": self.creator}
        return SupplierSurveillanceTemplateSerializer(
            instance, data=data, partial=partial, context=context
        )

    def test_create_with_nested_sections_and_items(self):
        serializer = self._serializer(
            data={
                "name": "General Supplier Review",
                "sections": [
                    {
                        "title": "Document Identification",
                        "order": 1,
                        "items": [
                            {"label": "Document reference correct", "item_type": "CHECK", "required": True, "order": 1},
                            {"label": "General observations", "item_type": "TEXTAREA", "required": False, "order": 2},
                        ],
                    }
                ],
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        template = serializer.save()
        self.assertEqual(template.sections.count(), 1)
        self.assertEqual(template.sections.first().items.count(), 2)

    def test_update_omitting_a_section_deletes_it(self):
        template = self._serializer(
            data={
                "name": "T",
                "sections": [
                    {"title": "A", "order": 1, "items": [{"label": "x", "item_type": "CHECK", "order": 1}]},
                    {"title": "B", "order": 2, "items": [{"label": "y", "item_type": "CHECK", "order": 1}]},
                ],
            }
        )
        template.is_valid()
        instance = template.save()
        self.assertEqual(instance.sections.count(), 2)

        kept_section = instance.sections.get(title="A")
        update = self._serializer(
            instance,
            data={
                "sections": [
                    {
                        "id": kept_section.id,
                        "title": "A",
                        "order": 1,
                        "items": [{"label": "x", "item_type": "CHECK", "order": 1}],
                    }
                ],
            },
            partial=True,
        )
        self.assertTrue(update.is_valid(), update.errors)
        updated = update.save()
        self.assertEqual(updated.sections.count(), 1)
        self.assertEqual(updated.sections.first().title, "A")

    def test_update_omitting_an_item_deletes_it_without_touching_the_section(self):
        template = self._serializer(
            data={
                "name": "T",
                "sections": [
                    {
                        "title": "A",
                        "order": 1,
                        "items": [
                            {"label": "x", "item_type": "CHECK", "order": 1},
                            {"label": "y", "item_type": "CHECK", "order": 2},
                        ],
                    }
                ],
            }
        )
        template.is_valid()
        instance = template.save()
        section = instance.sections.first()
        self.assertEqual(section.items.count(), 2)
        kept_item = section.items.get(label="x")

        update = self._serializer(
            instance,
            data={
                "sections": [
                    {
                        "id": section.id,
                        "title": "A",
                        "order": 1,
                        "items": [{"id": kept_item.id, "label": "x", "item_type": "CHECK", "order": 1}],
                    }
                ],
            },
            partial=True,
        )
        self.assertTrue(update.is_valid(), update.errors)
        updated = update.save()
        self.assertEqual(updated.sections.count(), 1)
        self.assertEqual(updated.sections.first().id, section.id)
        self.assertEqual(updated.sections.first().items.count(), 1)
        self.assertEqual(updated.sections.first().items.first().id, kept_item.id)

    def test_update_metadata_does_not_alter_an_existing_surveillances_snapshot(self):
        # 22 (L3C), exercised through the real editor serializer this time, not a direct model
        # save() -- the same guarantee must hold through the real write path.
        template = self.make_template(name="Original Name")
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator, template=template
        )
        update = self._serializer(template, data={"name": "Edited Name"}, partial=True)
        self.assertTrue(update.is_valid(), update.errors)
        update.save()
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.template_snapshot["template_name"], "Original Name")


ACCESS = "project_management.views.supplier_surveillance.get_register_document_or_404"
CAN_UPDATE = "project_management.views.supplier_surveillance.assert_can_update_document"


class GetSupplierSurveillanceApiTests(SupplierSurveillanceBase):
    # 19/20 (L3B) -- the minimal read/create endpoint, now with a Template.
    def _request(self, document, revision_id, method="get", data=None, action="retrieve"):
        factory = APIRequestFactory()
        request = (
            factory.post("/x/", data or {}, format="multipart")
            if method == "post"
            else factory.get("/x/")
        )
        force_authenticate(request, user=self.creator)
        actions = (
            {"get": action} if method == "get" else {"post": action}
        )
        view = SupplierSurveillanceViewSet.as_view(actions, permission_classes=[])
        with patch(ACCESS, return_value=document), patch(CAN_UPDATE):
            return view(request, document_id=document.id, revision_id=revision_id)

    def test_get_returns_null_before_creation_and_the_row_after(self):
        revision = self.make_supplier_revision()
        before = self._request(revision.document, revision.id)
        self.assertIsNone(before.data["supplier_surveillance"])
        create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template()
        )
        after = self._request(revision.document, revision.id)
        self.assertIsNotNone(after.data["supplier_surveillance"])
        self.assertTrue(after.data["supplier_surveillance"]["reference"].startswith("SR-"))

    def test_create_via_the_real_view_returns_201_with_the_reference(self):
        revision = self.make_supplier_revision()
        template = self.make_template()
        response = self._request(
            revision.document, revision.id, method="post", data={"template": template.id},
            action="create",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["reference"].startswith("SR-"))
        self.assertEqual(response.data["status"], "DRAFT")
        self.assertEqual(response.data["template_name"], template.name)
        self.assertEqual(response.data["template_version"], template.version)

    def test_create_without_a_template_is_rejected_with_400(self):
        revision = self.make_supplier_revision()
        response = self._request(
            revision.document, revision.id, method="post", data={}, action="create",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_via_the_real_view_rejects_a_second_one_with_400(self):
        revision = self.make_supplier_revision()
        template = self.make_template()
        self._request(
            revision.document, revision.id, method="post", data={"template": template.id},
            action="create",
        )
        second = self._request(
            revision.document, revision.id, method="post", data={"template": template.id},
            action="create",
        )
        self.assertEqual(second.status_code, 400)

    def test_wrong_revision_id_for_the_document_404s(self):
        revision = self.make_supplier_revision()
        # DRF's dispatch() catches Http404 and returns a 404 Response (DocumentWorkflowViewSet's
        # own _revision helper relies on the exact same behaviour) -- never a raised exception
        # at this level.
        response = self._request(revision.document, revision.id + 9999)
        self.assertEqual(response.status_code, 404)

    def test_templates_action_lists_only_active_templates_of_the_revisions_company(self):
        revision = self.make_supplier_revision()
        active = self.make_template(name="Active One")
        self.make_template(name="Inactive One", is_active=False)
        response = self._request(revision.document, revision.id, action="templates")
        names = [t["name"] for t in response.data["templates"]]
        self.assertIn("Active One", names)
        self.assertNotIn("Inactive One", names)
        self.assertEqual(len(names), 1)
        self.assertEqual(response.data["templates"][0]["id"], active.id)


class NoAutomaticCreationTests(SupplierSurveillanceBase):
    # 12/13 (L3B) / 28 (L3C) -- creating the Document/Revision alone must never create a
    # Surveillance, Template or otherwise.
    def test_no_automatic_surveillance_on_supplier_document_creation(self):
        self.create(
            document_origin=SUPPLIER,
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category="FOR_REVIEW",
        )
        self.assertEqual(SupplierSurveillance.objects.count(), 0)

    def test_no_automatic_surveillance_on_supplier_revision_creation(self):
        self.make_supplier_revision()
        self.assertEqual(SupplierSurveillance.objects.count(), 0)


class SupplierSurveillanceEventsApiTests(SupplierSurveillanceBase):
    """HISTORY-3 section 12/17: the existing per-revision supplier-surveillance retrieve
    endpoint now also exposes events[] (include_events=True, retrieve only -- never on the
    bulk Revision History list). Reuses the exact request helper GetSupplierSurveillanceApiTests
    already established."""

    def _request(self, document, revision_id):
        factory = APIRequestFactory()
        request = factory.get("/x/")
        force_authenticate(request, user=self.creator)
        view = SupplierSurveillanceViewSet.as_view({"get": "retrieve"}, permission_classes=[])
        with patch(ACCESS, return_value=document), patch(CAN_UPDATE):
            return view(request, document_id=document.id, revision_id=revision_id)

    def test_retrieve_exposes_events_in_chronological_order(self):
        revision = self.make_supplier_revision()
        create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template(),
        )
        response = self._request(revision.document, revision.id)
        events = response.data["supplier_surveillance"]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "SURVEILLANCE_CREATED")
        self.assertEqual(events[0]["actor"]["id"], self.creator.id)
        self.assertIsNotNone(events[0]["created_at"])
        timestamps = [event["created_at"] for event in events]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_events_belong_only_to_their_own_exact_surveillance(self):
        revision_a = self.make_supplier_revision(external_revision="A")
        revision_b = self.make_supplier_revision(external_revision="B")
        template = self.make_template()
        create_supplier_surveillance(
            document_revision=revision_a, actor=self.creator, template=template,
        )
        create_supplier_surveillance(
            document_revision=revision_b, actor=self.creator, template=template,
        )
        response_a = self._request(revision_a.document, revision_a.id)
        response_b = self._request(revision_b.document, revision_b.id)
        surveillance_a_id = response_a.data["supplier_surveillance"]["id"]
        surveillance_b_id = response_b.data["supplier_surveillance"]["id"]
        self.assertNotEqual(surveillance_a_id, surveillance_b_id)
        self.assertEqual(len(response_a.data["supplier_surveillance"]["events"]), 1)
        self.assertEqual(len(response_b.data["supplier_surveillance"]["events"]), 1)

    def test_bulk_revision_history_summary_never_includes_events(self):
        """The OTHER read path (DocumentRevisionViewSet.list, HISTORY-3's new `surveillance`
        summary key) must stay a summary -- never fetch events for every revision."""
        from project_management.views.supplier_surveillance import (
            serialize_supplier_surveillance,
        )

        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=self.make_template(),
        )
        summary = serialize_supplier_surveillance(surveillance)
        self.assertNotIn("events", summary)
