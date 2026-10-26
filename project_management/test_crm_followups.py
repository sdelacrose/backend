"""CRM-1B: manual commercial follow-up on Network records (targeted tests)."""

from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from companyprofileapis.models import Opportunity
from models.models import CompanyProfile, User
from project_management.models import ActionItem, ContactCompany, ContactUser, CrmInteraction
from project_management.serializers.contact import ContactUserSerializer
from project_management.signals import notify_on_contact_company_save, notify_on_contact_save
from project_management.test_action_item_query_services import ActionItemQueryFixtureMixin
from project_management.views.action_item import ActionItemViewSet
from project_management.views.crm import CrmInteractionViewSet


def _company(admin, name):
    company = CompanyProfile.objects.create(
        admin_user=admin, company_name=name, company_address="Street 1", company_town="Town",
        company_tell_no="1", company_mob_no="2", company_web_address="https://example.com",
        company_email_address=f"{name.replace(' ', '').lower()}@example.com",
        company_tax_reference=f"tax-{name}", company_iban=f"iban-{name}",
    )
    User.objects.filter(pk=admin.pk).update(company=company)
    admin.refresh_from_db()
    return company


class CrmFollowUpTests(ActionItemQueryFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        # Same isolation as the existing contact / opportunity tests: no notification side effects.
        for handler, sender in (
            (notify_on_contact_save, ContactUser),
            (notify_on_contact_company_save, ContactCompany),
        ):
            post_save.disconnect(handler, sender=sender)
            self.addCleanup(post_save.connect, handler, sender=sender)
        notification_patcher = patch("ecpmpapp.utils.should_notify_user_comprehensive", return_value=False)
        notification_patcher.start()
        self.addCleanup(notification_patcher.stop)
        self.factory = APIRequestFactory()
        self.today = timezone.localdate()
        self.acme = ContactCompany.objects.create(
            company_profile=self.company, company_name="ACME", contact_email="acme@example.com",
        )
        self.alice = ContactUser.objects.create(
            company_profile=self.company, name="Alice", surname="Martin",
            email="alice@example.com", contact_company=self.acme,
        )
        self.opportunity = Opportunity.objects.create(
            title="Retrofit", company_profile=self.company, created_by=self.user,
            customer_user=self.alice, customer_company=self.acme,
        )
        # A second, unrelated tenant.
        self.other_user = User.objects.create_user(
            email="other-crm@example.com", password="password", first_name="Other",
            last_name="Tenant", user_type="company_user",
        )
        self.other_company = _company(self.other_user, "Other Co")
        self.foreign_contact = ContactUser.objects.create(
            company_profile=self.other_company, name="Bob", surname="Foreign", email="bob@example.com",
        )
        self.foreign_company = ContactCompany.objects.create(
            company_profile=self.other_company, company_name="Foreign", contact_email="f@example.com",
        )
        self.foreign_opportunity = Opportunity.objects.create(
            title="Foreign deal", company_profile=self.other_company, created_by=self.other_user,
        )

    # ---- helpers ---------------------------------------------------------------------------
    def _permissions(self, *, contact=True, opportunity=True):
        stack = ExitStack()
        for target in (
            "project_management.views.action_item.require_any_permission_multiple_modules",
            "project_management.views.crm.require_any_permission_multiple_modules",
        ):
            stack.enter_context(patch(target, return_value=True))
        stack.enter_context(patch(
            "companyprofileapis.views.opportunity.require_any_permission_multiple_modules",
            return_value=opportunity,
        ))
        for name in ("has_permission", "has_object_permission"):
            stack.enter_context(patch(
                f"project_management.permissions.ActionItemPermission.{name}", return_value=True,
            ))
        stack.enter_context(patch(
            "project_management.permissions.ContactPermission._check_permission", return_value=contact,
        ))
        return stack

    def call(self, viewset, actions, method, path, data=None, user=None, pk=None, **perms):
        request = getattr(self.factory, method)(path, data or {}, format="json")
        force_authenticate(request, user=user or self.user)
        view = viewset.as_view(actions)
        with self._permissions(**perms):
            return view(request, pk=pk) if pk else view(request)

    def create_follow_up(self, user=None, **links):
        payload = {
            "name": "Relance devis", "company_profile": (user or self.user).company_id,
            "status": "open", "target_date": str(self.today), "assignees": [(user or self.user).id],
            **links,
        }
        return self.call(ActionItemViewSet, {"post": "create"}, "post", "/action_items/", payload, user=user)

    def my_actions(self, bucket, **params):
        return self.call(
            ActionItemViewSet, {"get": "time"}, "get", "/action_items/time/",
            {"bucket": bucket, "related_to_me": "true", "open_only": "true", "page_size": 50, **params},
        )

    def interaction(self, **payload):
        body = {"type": "note", "occurred_at": timezone.now().isoformat(), "summary": "Échange", **payload}
        return self.call(CrmInteractionViewSet, {"post": "create"}, "post", "/crm-interactions/", body)

    def summary(self, **scope):
        return self.call(CrmInteractionViewSet, {"get": "summary"}, "get", "/crm-interactions/summary/", scope)

    def ids(self, response):
        return {row["id"] for row in response.data["results"]}

    # ---- 1-3: follow-ups linked to Network records ------------------------------------------
    def test_01_follow_up_linked_to_contact(self):
        response = self.create_follow_up(contact_users=[self.alice.id])
        self.assertEqual(response.status_code, 201, response.data)
        item = ActionItem.objects.get(pk=response.data["id"])
        self.assertEqual(list(item.contact_users.all()), [self.alice])
        self.assertEqual(response.data["contact_users"], [{"id": self.alice.id, "name": "Alice Martin"}])

    def test_02_follow_up_linked_directly_to_company(self):
        response = self.create_follow_up(contact_companies=[self.acme.id])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(list(ActionItem.objects.get(pk=response.data["id"]).contact_companies.all()), [self.acme])

    def test_03_follow_up_linked_to_existing_opportunity(self):
        response = self.create_follow_up(opportunities=[self.opportunity.id], contact_users=[self.alice.id])
        self.assertEqual(response.status_code, 201, response.data)
        item = ActionItem.objects.get(pk=response.data["id"])
        self.assertEqual(list(item.opportunities.all()), [self.opportunity])
        # The opportunity keeps its own Network relations untouched.
        self.opportunity.refresh_from_db()
        self.assertEqual((self.opportunity.customer_user, self.opportunity.customer_company), (self.alice, self.acme))

    # ---- 4-7: Mes actions, postpone, close --------------------------------------------------
    def test_04_to_07_my_actions_postpone_close(self):
        item_id = self.create_follow_up(contact_users=[self.alice.id]).data["id"]

        today = self.my_actions("today")
        self.assertEqual(today.status_code, 200)
        self.assertIn(item_id, self.ids(today))
        card = next(r for r in today.data["results"] if r["id"] == item_id)
        self.assertEqual(card["crm_links"]["contact_users"][0]["id"], self.alice.id)

        # 5. Postpone to next month -> leaves "today", shows under "upcoming".
        new_date = self.today + timedelta(days=40)
        patched = self.call(
            ActionItemViewSet, {"patch": "partial_update"}, "patch", f"/action_items/{item_id}/",
            {"target_date": str(new_date)}, pk=item_id,
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertNotIn(item_id, self.ids(self.my_actions("today")))
        self.assertIn(item_id, self.ids(self.my_actions("upcoming")))

        # 6. Close -> closed_at / closed_by recorded.
        closed = self.call(
            ActionItemViewSet, {"patch": "partial_update"}, "patch", f"/action_items/{item_id}/",
            {"status": "closed"}, pk=item_id,
        )
        self.assertEqual(closed.status_code, 200, closed.data)
        item = ActionItem.objects.get(pk=item_id)
        self.assertIsNotNone(item.closed_at)
        self.assertEqual(item.closed_by, self.user)

        # 7. Gone from every open bucket, listed under "done".
        for bucket in ("late", "today", "this_week", "upcoming"):
            self.assertNotIn(item_id, self.ids(self.my_actions(bucket)), bucket)
        self.assertIn(item_id, self.ids(self.my_actions("done")))

    def test_late_bucket_and_type_filter(self):
        late_id = self.create_follow_up(contact_companies=[self.acme.id]).data["id"]
        ActionItem.objects.filter(pk=late_id).update(target_date=self.today - timedelta(days=3))
        self.assertIn(late_id, self.ids(self.my_actions("late")))
        self.assertIn(late_id, self.ids(self.my_actions("late", crm_link="company")))
        self.assertNotIn(late_id, self.ids(self.my_actions("late", crm_link="contact")))

    # ---- 8-10: interactions ------------------------------------------------------------------
    def test_08_to_10_interactions(self):
        created = {}
        for kind in ("note", "call", "meeting"):
            response = self.interaction(type=kind, contact_user=self.alice.id, opportunity=self.opportunity.id)
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(response.data["author"], self.user.id)
            created[kind] = response.data["id"]

        # 9. Shown once on each linked record (contact, its company, the opportunity).
        for scope in ({"contact_user": self.alice.id}, {"contact_company": self.acme.id},
                      {"opportunity": self.opportunity.id}):
            listing = self.call(CrmInteractionViewSet, {"get": "list"}, "get", "/crm-interactions/", scope)
            self.assertEqual(listing.status_code, 200)
            ids = [row["id"] for row in listing.data]
            self.assertEqual(sorted(ids), sorted(created.values()), scope)
            self.assertEqual(len(ids), len(set(ids)))

        # 10. Persisted: a fresh request (new query) reads them back; summary uses them.
        self.assertEqual(CrmInteraction.objects.filter(pk__in=created.values()).count(), 3)
        summary = self.summary(contact_user=self.alice.id)
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["interactions_count"], 3)
        self.assertIsNotNone(summary.data["last_interaction"])
        self.assertEqual(summary.data["opportunities"][0]["id"], self.opportunity.id)

    def test_interaction_requires_a_business_link(self):
        response = self.interaction()
        self.assertEqual(response.status_code, 400)
        self.assertIn("links", response.data)

    def test_summary_next_action_and_owner(self):
        self.alice.owner = self.user
        self.alice.save()
        later = self.create_follow_up(contact_users=[self.alice.id]).data["id"]
        ActionItem.objects.filter(pk=later).update(target_date=self.today + timedelta(days=9))
        sooner = self.create_follow_up(contact_users=[self.alice.id]).data["id"]
        summary = self.summary(contact_user=self.alice.id)
        self.assertEqual(summary.data["next_action"]["id"], sooner)
        self.assertEqual(summary.data["owner"]["id"], self.user.id)
        # The company sees the follow-ups of its contacts.
        company_summary = self.summary(contact_company=self.acme.id)
        self.assertEqual({a["id"] for a in company_summary.data["open_actions"]}, {later, sooner})

    # ---- 11-12: tenant isolation and permissions ---------------------------------------------
    def test_11_tenant_isolation(self):
        for links in ({"contact_users": [self.foreign_contact.id]},
                      {"contact_companies": [self.foreign_company.id]},
                      {"opportunities": [self.foreign_opportunity.id]}):
            self.assertEqual(self.create_follow_up(**links).status_code, 400, links)
        for payload in ({"contact_user": self.foreign_contact.id},
                        {"contact_company": self.foreign_company.id},
                        {"opportunity": self.foreign_opportunity.id}):
            self.assertEqual(self.interaction(**payload).status_code, 400, payload)
        # Model-level guard (trusted code paths) refuses cross-tenant links too.
        with self.assertRaises(Exception):
            CrmInteraction.objects.create(
                type="note", occurred_at=timezone.now(), summary="x",
                company_profile=self.company, contact_user=self.foreign_contact,
            )
        # Other tenant's interactions are never listed.
        CrmInteraction.objects.create(
            type="call", occurred_at=timezone.now(), summary="foreign",
            company_profile=self.other_company, contact_user=self.foreign_contact,
        )
        listing = self.call(CrmInteractionViewSet, {"get": "list"}, "get", "/crm-interactions/",
                            {"contact_user": self.foreign_contact.id})
        self.assertEqual(listing.data, [])
        self.assertEqual(self.summary(contact_user=self.foreign_contact.id).status_code, 404)
        # Owner must belong to the same company.
        serializer = ContactUserSerializer(
            self.alice, data={"owner": self.other_user.id}, partial=True,
            context={"request": type("R", (), {"user": self.user})()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("owner", serializer.errors)

    def test_12_permissions_on_linked_objects(self):
        # No contact_management right -> interactions refused.
        denied = self.call(CrmInteractionViewSet, {"get": "list"}, "get", "/crm-interactions/",
                           {"contact_user": self.alice.id}, contact=False)
        self.assertEqual(denied.status_code, 403)
        created = self.call(
            CrmInteractionViewSet, {"post": "create"}, "post", "/crm-interactions/",
            {"type": "note", "occurred_at": timezone.now().isoformat(), "summary": "x",
             "contact_user": self.alice.id}, contact=False,
        )
        self.assertEqual(created.status_code, 403)
        # Interaction on an opportunity the user cannot see stays hidden and cannot be created.
        CrmInteraction.objects.create(
            type="note", occurred_at=timezone.now(), summary="deal note",
            company_profile=self.company, opportunity=self.opportunity, contact_user=self.alice,
        )
        hidden = self.call(CrmInteractionViewSet, {"get": "list"}, "get", "/crm-interactions/",
                           {"contact_user": self.alice.id}, opportunity=False)
        self.assertEqual(hidden.data, [])
        refused = self.call(
            CrmInteractionViewSet, {"post": "create"}, "post", "/crm-interactions/",
            {"type": "note", "occurred_at": timezone.now().isoformat(), "summary": "x",
             "opportunity": self.opportunity.id}, opportunity=False,
        )
        self.assertEqual(refused.status_code, 400)

    # ---- 13: existing data preserved ----------------------------------------------------------
    def test_13_existing_data_preserved(self):
        legacy = ActionItem.objects.create(
            name="Legacy", company_profile=self.company, status="open", target_date=self.today,
        )
        legacy.opportunities.add(self.opportunity)
        legacy.assignees.add(self.user)
        # Adding a contact through the API keeps the existing opportunity link.
        response = self.call(
            ActionItemViewSet, {"patch": "partial_update"}, "patch", f"/action_items/{legacy.id}/",
            {"contact_users": [self.alice.id]}, pk=legacy.id,
        )
        self.assertEqual(response.status_code, 200, response.data)
        legacy.refresh_from_db()
        self.assertEqual(list(legacy.opportunities.all()), [self.opportunity])
        self.assertEqual(list(legacy.contact_users.all()), [self.alice])
        # A contact without owner still serializes as before (+ owner_detail None).
        data = ContactUserSerializer(self.acme.linked_contact_users.first(),
                                     context={"request": type("R", (), {"user": self.user})()}).data
        self.assertIsNone(data["owner"])
        self.assertIsNone(data["owner_detail"])
