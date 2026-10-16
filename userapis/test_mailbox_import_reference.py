"""MAIL-1.3D-C2-B2: an IN imported from Outlook may have no sender's reference -- and ONLY there.
Graph is a fake; the numbering is the existing correspondence counter."""

from rest_framework.test import APIClient

from project_management.models import Correspondence, CorrespondenceReferenceCounter
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.services.correspondances_register import build_register_queryset, serialize_row
from userapis.models import MailboxMessage
from userapis.test_mailbox_import import ImportBase

IMPORT_URL = "/api/user/mailbox-import/"
CORRESPONDENCE_URL = "/api/project_management/correspondence"


class ReferenceBase(ImportBase):
    def post_import(self, raw, client=None, **extra):
        """The import request, exactly as sent: `extra` is the whole of what follows the three
        mandatory keys (so a test can leave external_reference out altogether)."""
        client = client or self.client
        client.force_authenticate(self.owner)
        return client.post(
            IMPORT_URL,
            {"reference": self.reference(self.owner, raw), "project": self.project.pk, "mode": "create", **extra},
            format="json",
        )

    def import_in(self, n, **extra):
        return self.post_import(self.graph.add(n, subject=f"Mail {n}"), **extra)

    def corr(self, response):
        return Correspondence.objects.get(pk=response.data["correspondence"]["id"])

    def counter(self):
        return CorrespondenceReferenceCounter.objects.get(project=self.project).last_sequence

    def code(self):
        self.project.refresh_from_db()
        return self.project.project_code


class CreateTests(ReferenceBase):
    def test_a_sender_reference_keeps_the_existing_behaviour(self):
        response = self.import_in(1, external_reference="  CLIENT-77 ")
        self.assertEqual(response.status_code, 201, response.data)
        corr = self.corr(response)
        self.assertEqual((corr.external_reference, corr.reference, corr.display_reference), ("CLIENT-77", "", "CLIENT-77"))
        self.assertFalse(CorrespondenceReferenceCounter.objects.filter(project=self.project).exists())   # nothing consumed

    def test_without_one_the_in_gets_a_links_reference_and_no_invented_external_one(self):
        for n, extra in enumerate(({}, {"external_reference": ""}, {"external_reference": "   "}), start=1):
            response = self.import_in(n, **extra)
            self.assertEqual(response.status_code, 201, (extra, response.data))
            corr = self.corr(response)
            self.assertEqual(corr.external_reference or "", "")                      # never fabricated
            self.assertEqual(corr.reference, f"{self.code()}-COR-{n:05d}")           # the existing format and counter
            self.assertEqual(corr.display_reference, corr.reference)
            self.assertEqual(response.data["correspondence"]["reference"], corr.reference)
            self.assertEqual((corr.direction, corr.status, corr.project_id), ("IN", "ISSUED", self.project.pk))

    def test_the_counter_is_the_one_shared_with_out(self):
        first_out = self.create_out_project()
        imported = self.import_in(1)
        second_out = self.create_out_project()
        code = self.code()
        self.assertEqual(first_out.reference, f"{code}-COR-00001")
        self.assertEqual(self.corr(imported).reference, f"{code}-COR-00002")
        self.assertEqual(second_out.reference, f"{code}-COR-00003")                  # OUT numbering carries on, unchanged
        self.assertEqual(self.counter(), 3)

    def test_imports_without_a_reference_never_collide(self):
        first, second = self.graph.add(1, subject="One"), self.graph.add(2, subject="Two")
        racing = []

        def competing_request():
            racing.append(self.post_import(second, client=APIClient()))

        self.graph.on_message_get = competing_request            # the second import completes inside the first
        response = self.post_import(first)
        self.assertEqual((response.status_code, racing[0].status_code), (201, 201))
        refs = sorted(Correspondence.objects.filter(direction="IN").values_list("reference", flat=True))
        self.assertEqual(refs, [f"{self.code()}-COR-00001", f"{self.code()}-COR-00002"])

    def test_a_retry_creates_no_duplicate_and_consumes_no_number(self):
        raw = self.graph.add(1)
        first = self.post_import(raw)
        again = self.post_import(raw)
        self.assertEqual((again.status_code, again.data["result"]), (200, "already_imported"))
        self.assertEqual(again.data["correspondence"]["id"], first.data["correspondence"]["id"])
        self.assertEqual((Correspondence.objects.filter(direction="IN").count(), self.counter()), (1, 1))

    def test_a_refused_import_burns_no_number(self):
        response = self.import_in(1, correspondence_type="Nonsense")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CorrespondenceReferenceCounter.objects.filter(last_sequence__gt=0).count(), 0)   # rolled back with it
        self.assertEqual(MailboxMessage.objects.filter(state="imported").count(), 0)

    def test_parent_child_links_and_response_indicators_still_work(self):
        out = self.make_out(response_required=True, response_status="AWAITING_REPLY")
        response = self.import_in(1, parent_correspondence=out.pk)
        self.assertEqual(response.status_code, 201, response.data)
        child = self.corr(response)
        out.refresh_from_db()
        self.assertEqual((child.parent_correspondence_id, out.response_status), (out.pk, "ANSWERED"))
        shown = CorrespondenceSerializer(out, context={"user": self.owner}).data
        self.assertEqual([r["reference"] for r in shown["responses"]], [child.reference])
        self.assertEqual(CorrespondenceSerializer(child, context={"user": self.owner}).data["parent_reference"], out.display_reference)


class OtherPathsUnchangedTests(ReferenceBase):
    def serializer(self, **data):
        payload = {"direction": "IN", "subject": "Classic", "project": self.project.pk, **data}
        return CorrespondenceSerializer(data=payload, context={"user": self.owner})

    def test_a_classic_in_still_needs_a_sender_reference(self):
        serializer = self.serializer()
        self.assertFalse(serializer.is_valid())
        self.assertIn("external_reference", serializer.errors)
        self.assertFalse(self.serializer(external_reference="  ").is_valid())

    def test_a_client_cannot_switch_the_exception_on(self):
        self.assertFalse(self.serializer(allow_missing_external_reference=True).is_valid())    # a body field is just ignored
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            f"{CORRESPONDENCE_URL}?allow_missing_external_reference=true",
            {"direction": "IN", "subject": "Classic", "project": self.project.pk, "allow_missing_external_reference": True},
            format="json",
        )
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertIn("external_reference", response.data)
        self.assertEqual(Correspondence.objects.count(), 0)

    def test_out_creation_and_numbering_are_unchanged(self):
        out = self.create_out_project()
        self.assertEqual(out.reference, f"{self.code()}-COR-00001")
        self.assertFalse((out.external_reference or "").strip())                      # an OUT never needed one
        self.assertTrue(CorrespondenceSerializer(
            data={"direction": "OUT", "status": "DRAFT", "subject": "x", "project": self.project.pk,
                  "recipient_company": self.client_company.pk}, context={"user": self.owner},
        ).is_valid())


class UpdateTests(ReferenceBase):
    def imported(self):
        return self.corr(self.import_in(1))

    def update(self, corr, **data):
        serializer = CorrespondenceSerializer(corr, data=data, partial=True, context={"user": self.owner})
        return serializer, serializer.is_valid()

    def test_another_field_can_change_even_when_the_blank_reference_is_resent(self):
        corr = self.imported()
        for payload in ({"subject": "Renamed"}, {"subject": "Again", "external_reference": ""}, {"subject": "Third", "external_reference": None}):
            serializer, valid = self.update(corr, **payload)
            self.assertTrue(valid, (payload, serializer.errors))
            corr = serializer.save()
        corr.refresh_from_db()
        self.assertEqual((corr.subject, corr.reference), ("Third", f"{self.code()}-COR-00001"))

    def test_a_real_reference_can_be_added_later_and_the_links_reference_is_kept(self):
        corr = self.imported()
        before = corr.reference
        self.assertEqual(corr.display_reference, before)                                  # before
        serializer, valid = self.update(corr, external_reference="CLIENT-9")
        self.assertTrue(valid, serializer.errors)
        corr = serializer.save()
        corr.refresh_from_db()
        self.assertEqual((corr.reference, corr.external_reference), (before, "CLIENT-9"))   # internal reference untouched
        self.assertEqual(corr.display_reference, "CLIENT-9")                                # after: the sender's reference leads

    def test_a_reference_once_entered_can_be_replaced_but_never_cleared(self):
        corr = self.update(self.imported(), external_reference="CLIENT-9")[0].save()
        serializer, valid = self.update(corr, external_reference="CLIENT-10")
        self.assertTrue(valid)
        corr = serializer.save()
        for blank in ("", "  ", None):
            serializer, valid = self.update(corr, external_reference=blank)
            self.assertFalse(valid, blank)
            self.assertIn("external_reference", serializer.errors)
        corr.refresh_from_db()
        self.assertEqual(corr.external_reference, "CLIENT-10")

    def test_the_exemption_is_not_general(self):
        classic = self.create(direction="IN", project=self.project.id, external_reference="CLASSIC-1")
        self.assertFalse(self.update(classic, external_reference="")[1])
        historical = Correspondence.objects.create(
            direction="IN", project=self.project, subject="Old", status="ISSUED",
            reference="OLD-COR-00001", response_status="NOT_REQUIRED",
        )
        self.assertFalse(self.update(historical, external_reference="")[1])               # no Outlook source: rule unchanged
        self.assertEqual(self.do(self.owner, self.reference(self.owner, self.graph.add(5)), mode="attach", target_correspondence=classic.pk).status_code, 201)
        self.assertFalse(self.update(classic, external_reference="")[1])                  # attached source does not unlock it

    def test_the_edit_permissions_are_the_existing_ones(self):
        corr = self.imported()
        self.client.force_authenticate(self.approver)                                      # no correspondence right
        response = self.client.patch(f"{CORRESPONDENCE_URL}/{corr.pk}", {"subject": "Hijack"}, format="json")
        self.assertIn(response.status_code, (403, 404))
        corr.refresh_from_db()
        self.assertNotEqual(corr.subject, "Hijack")


class ListAndSearchTests(ReferenceBase):
    def test_lists_details_search_and_the_register_show_the_links_reference(self):
        corr = self.corr(self.import_in(1))
        self.client.force_authenticate(self.owner)
        found = self.client.get(CORRESPONDENCE_URL, {"search": corr.reference})
        rows = found.data["results"] if "results" in found.data else found.data
        self.assertEqual([r["id"] for r in rows], [corr.pk])
        detail = self.client.get(f"{CORRESPONDENCE_URL}/{corr.pk}")
        self.assertEqual((detail.status_code, detail.data["reference"], detail.data["external_reference"] or ""), (200, corr.reference, ""))
        self.assertEqual(self.register_rows(corr.reference, corr), [corr.reference])
        # once the sender's reference is added, both references find it and the sender's one leads
        serializer = CorrespondenceSerializer(corr, data={"external_reference": "CLIENT-9"}, partial=True, context={"user": self.owner})
        self.assertTrue(serializer.is_valid())
        serializer.save()
        for term in (corr.reference, "CLIENT-9"):
            self.assertEqual(self.register_rows(term, corr), ["CLIENT-9"], term)

    def register_rows(self, term, corr):
        """The unified register's own query and row shape (the HTTP route also runs a licence check
        that needs tables this test database does not have)."""
        queryset = build_register_queryset(
            self.owner, project=self.project, company_scope=False, entity_types=("CORRESPONDENCE",),
            direction=None, company=None, contact=None, correspondence_type=None, search=term,
            status=None, ordering=[],
        )
        return [row["reference"] for row in (serialize_row(r) for r in queryset) if row["id"] == corr.pk]
