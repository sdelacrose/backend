"""MAIL-1.5-B: the company's library of e-mail templates, the variables engine, the read-only
"apply" endpoint, and the guard that stops a send while a template placeholder is still unresolved.

Nothing here reaches a real mailbox: sends go through Django's locmem mail backend, the Microsoft 365
sender is mocked, and every test runs inside the test runner's own isolated database.
"""
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from rest_framework.test import APIClient

from project_management.models import (
    Correspondence, CorrespondenceSendEvent, CorrespondenceTemplate, CorrespondenceValidationWorkflow,
)
from project_management.services.correspondence_send import SendBlocked, send_correspondence
from project_management.services.correspondence_template import (
    MARKER_RE, VARIABLES, find_markers, render_text, unresolved_marker_errors, validate_template_fields,
)
from project_management.services.correspondence_template_starters import STARTER_TEMPLATES
from project_management.test_correspondence import grant_correspondence_permissions
from project_management.test_correspondence_send import SendTestBase
from project_management.test_documents_full_access import grant_company_permission, grant_project_permission
from userapis.test_mailbox import _make_company, _make_user

BASE = "/api/project_management/correspondence-templates"
SEND_BASE = "/api/project_management/correspondence"


def template_data(**extra):
    return {
        "name": "Transmission", "description": "", "language": "FR",
        "subject_template": "Documents - {{project_name}}",
        "body_template": "Bonjour {{recipient_name}},\n\nCi-joint les documents du projet {{project_name}}.",
        **extra,
    }


class TemplateBase(SendTestBase):
    def setUp(self):
        super().setUp()
        license_patch = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        license_patch.start()
        self.addCleanup(license_patch.stop)
        # The recipient fixture of SendTestBase carries no owner company: give it the tenant a real contact has.
        self.recipient_contact.company_profile = self.company
        self.recipient_contact.save(update_fields=["company_profile"])
        # self.owner: view/create/update/delete (+ the send ones below) -- a plain AUTHOR, no template right.
        grant_correspondence_permissions(self.owner, self.company, actions=("send",))
        self.manager = self._company_member("tplmanager")
        grant_correspondence_permissions(self.manager, self.company, actions=("template_manage",))
        self.full = self._company_member("tplfull")
        grant_company_permission(self.full, self.company, ["documents.full_access"], "Template Full Access")
        self.nobody = self._company_member("tplnobody")
        self.foreign = _make_user("foreign-tpl@example.com", _make_company("Foreign Template Co"))
        grant_correspondence_permissions(
            self.foreign, self.foreign.company, actions=("view", "create", "template_manage"),
        )

    def api(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def make(self, company=None, **extra):
        data = template_data(**extra)
        data.pop("description", None)
        return CorrespondenceTemplate.objects.create(
            company_profile=company or self.company, created_by=self.manager, **data,
        )

    def apply(self, user, template, **body):
        return self.api(user).post(f"{BASE}/{template.pk}/apply", body, format="json")


class TemplateDataTests(TemplateBase):
    def test_a_valid_template_is_created_for_the_requesters_company(self):
        response = self.api(self.manager).post(BASE, template_data(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        template = CorrespondenceTemplate.objects.get(pk=response.data["id"])
        self.assertEqual(template.company_profile_id, self.company.id)
        self.assertEqual(template.created_by_id, self.manager.id)
        self.assertTrue(template.is_active)

    def test_the_company_can_never_be_chosen_by_the_client(self):
        response = self.api(self.manager).post(
            BASE, template_data(company_profile=self.foreign.company.id), format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            CorrespondenceTemplate.objects.get(pk=response.data["id"]).company_profile_id, self.company.id,
        )

    def test_the_language_is_one_of_fr_en_de(self):
        for language in ("FR", "EN", "DE"):
            response = self.api(self.manager).post(
                BASE, template_data(name=f"T {language}", language=language), format="json",
            )
            self.assertEqual(response.status_code, 201, language)
        response = self.api(self.manager).post(BASE, template_data(name="Bad", language="ES"), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("language", response.data)
        with self.assertRaises(ValidationError):
            self.make(name="Bad ORM", language="IT")

    def test_the_name_is_unique_inside_a_company_only(self):
        self.make(name="Relance")
        again = self.api(self.manager).post(BASE, template_data(name="relance"), format="json")
        self.assertEqual(again.status_code, 400)
        self.assertIn("name", again.data)
        with self.assertRaises((ValidationError, IntegrityError)):
            self.make(name="Relance")
        other = self.api(self.foreign).post(BASE, template_data(name="Relance"), format="json")
        self.assertEqual(other.status_code, 201)

    def test_a_template_is_deactivated_and_reactivated_never_deleted(self):
        template = self.make()
        off = self.api(self.manager).patch(f"{BASE}/{template.pk}", {"is_active": False}, format="json")
        self.assertEqual(off.status_code, 200)
        template.refresh_from_db()
        self.assertFalse(template.is_active)
        on = self.api(self.manager).patch(f"{BASE}/{template.pk}", {"is_active": True}, format="json")
        self.assertTrue(on.data["is_active"])
        self.assertEqual(self.api(self.manager).delete(f"{BASE}/{template.pk}").status_code, 405)
        self.assertTrue(CorrespondenceTemplate.objects.filter(pk=template.pk).exists())

    def test_every_edit_is_kept_in_the_history(self):
        template = self.make()
        self.api(self.manager).patch(f"{BASE}/{template.pk}", {"name": "Renamed"}, format="json")
        self.assertEqual(template.history.count(), 2)

    def test_the_three_starter_templates_are_valid_french_plain_text_and_not_inserted_anywhere(self):
        self.assertEqual(
            [t["name"] for t in STARTER_TEMPLATES], ["Transmission documentaire", "Demande d'information", "Relance"],
        )
        for starter in STARTER_TEMPLATES:
            self.assertEqual(starter["language"], "FR")
            validate_template_fields(
                name=starter["name"], subject_template=starter["subject_template"],
                body_template=starter["body_template"],
            )
            for text in (starter["subject_template"], starter["body_template"]):
                self.assertTrue(set(find_markers(text)) <= set(VARIABLES))
                self.assertNotIn("@", text)          # no recipient
        self.assertEqual(CorrespondenceTemplate.objects.count(), 0)


class TemplateValidationTests(TemplateBase):
    def assert_refused(self, **extra):
        response = self.api(self.manager).post(BASE, template_data(**extra), format="json")
        self.assertEqual(response.status_code, 400, response.content)
        return response.data

    def test_an_unknown_variable_is_refused_in_the_subject_and_in_the_body(self):
        self.assertIn("subject_template", self.assert_refused(subject_template="Hello {{sender_name}}"))
        errors = self.assert_refused(body_template="Today is {{today}}")
        self.assertIn("body_template", errors)
        self.assertIn("{{today}}", str(errors["body_template"]))
        with self.assertRaises(ValidationError):
            self.make(name="Unknown ORM", body_template="x {{nope}}")

    def test_a_malformed_placeholder_is_refused(self):
        for body in ("Hello {{ recipient name }}", "Hello {{recipient_name}", "Hello {{}}", "Hello }} {{"):
            self.assertIn("body_template", self.assert_refused(body_template=body), body)

    def test_html_is_refused_but_angle_brackets_in_text_are_not(self):
        self.assertIn("body_template", self.assert_refused(body_template="Hello <b>{{project_name}}</b>"))
        self.assertIn("body_template", self.assert_refused(body_template="<script>alert(1)</script>"))
        self.assertIn("subject_template", self.assert_refused(subject_template="Hi <!-- x --> there"))
        response = self.api(self.manager).post(
            BASE, template_data(body_template="Write to <jane@x.com> if 1 < 2 and 3 > 2."), format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)

    def test_the_subject_is_one_line_and_not_too_long_and_nothing_is_empty(self):
        self.assertIn("subject_template", self.assert_refused(subject_template="Line one\nLine two"))
        self.assertIn("subject_template", self.assert_refused(subject_template="x" * 501))
        self.assertIn("subject_template", self.assert_refused(subject_template="   "))
        self.assertIn("body_template", self.assert_refused(body_template=""))
        self.assertIn("name", self.assert_refused(name="   "))

    def test_a_valid_edit_keeps_working_and_an_invalid_edit_changes_nothing(self):
        template = self.make()
        bad = self.api(self.manager).patch(f"{BASE}/{template.pk}", {"body_template": "{{unknown}}"}, format="json")
        self.assertEqual(bad.status_code, 400)
        template.refresh_from_db()
        self.assertIn("{{recipient_name}}", template.body_template)


class TemplatePermissionTests(TemplateBase):
    def test_an_author_lists_and_reads_the_active_templates_of_the_company_but_cannot_change_them(self):
        active, inactive = self.make(name="On"), self.make(name="Off", is_active=False)
        listed = self.api(self.owner).get(BASE)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([t["name"] for t in listed.data], ["On"])
        self.assertEqual(self.api(self.owner).get(f"{BASE}/{active.pk}").status_code, 200)
        self.assertEqual(self.api(self.owner).get(f"{BASE}/{inactive.pk}").status_code, 404)
        self.assertEqual(self.api(self.owner).get(BASE, {"include_inactive": "true"}).status_code, 403)
        self.assertEqual(self.api(self.owner).post(BASE, template_data(name="New"), format="json").status_code, 403)
        self.assertEqual(
            self.api(self.owner).patch(f"{BASE}/{active.pk}", {"name": "Hacked"}, format="json").status_code, 403,
        )
        active.refresh_from_db()
        self.assertEqual(active.name, "On")

    def test_creating_correspondences_never_gives_the_right_to_manage_the_library(self):
        for action in (("view", "create", "update", "delete", "send"),):
            grant_correspondence_permissions(self.nobody, self.company, actions=action)
        self.assertEqual(self.api(self.nobody).post(BASE, template_data(name="X"), format="json").status_code, 403)

    def test_a_manager_sees_the_inactive_templates_and_manages_them(self):
        self.make(name="On")
        off = self.make(name="Off", is_active=False)
        listed = self.api(self.manager).get(BASE, {"include_inactive": "true"})
        self.assertEqual(sorted(t["name"] for t in listed.data), ["Off", "On"])
        self.assertEqual(self.api(self.manager).get(f"{BASE}/{off.pk}").status_code, 200)

    def test_documents_full_access_manages_the_library_like_everywhere_in_documents(self):
        response = self.api(self.full).post(BASE, template_data(name="By full access"), format="json")
        self.assertEqual(response.status_code, 201)

    def test_nobody_without_a_right_sees_or_applies_anything(self):
        template = self.make()
        self.assertEqual(self.api(self.nobody).get(BASE).status_code, 403)
        self.assertEqual(self.api(self.nobody).get(f"{BASE}/{template.pk}").status_code, 403)
        self.assertEqual(self.apply(self.nobody, template).status_code, 403)

    def test_the_language_filter(self):
        self.make(name="FR one", language="FR")
        self.make(name="EN one", language="EN")
        listed = self.api(self.owner).get(BASE, {"language": "en"})
        self.assertEqual([t["name"] for t in listed.data], ["EN one"])
        self.assertEqual(self.api(self.owner).get(BASE, {"language": "XX"}).status_code, 400)

    def test_the_right_to_create_can_come_from_the_project_role(self):
        self.make()
        member = self._company_member("tplprojectonly")
        grant_project_permission(
            member, self.project, self.company, ["documents.correspondence_create"], "Template Project Role",
        )
        self.assertEqual(self.api(member).get(BASE).status_code, 403)                       # no company role
        self.assertEqual(self.api(member).get(BASE, {"project": self.project.id}).status_code, 200)

    def test_companies_are_isolated(self):
        mine = self.make(name="Mine")
        theirs = self.make(name="Theirs", company=self.foreign.company)
        foreign_list = self.api(self.foreign).get(BASE, {"include_inactive": "true"})
        self.assertEqual([t["name"] for t in foreign_list.data], ["Theirs"])
        own_list = self.api(self.manager).get(BASE, {"include_inactive": "true"})
        self.assertEqual([t["name"] for t in own_list.data], ["Mine"])
        # a direct request with another company's id: detail, edit and apply are plain 404s
        self.assertEqual(self.api(self.manager).get(f"{BASE}/{theirs.pk}").status_code, 404)
        self.assertEqual(
            self.api(self.manager).patch(f"{BASE}/{theirs.pk}", {"name": "Taken"}, format="json").status_code, 404,
        )
        self.assertEqual(self.apply(self.owner, theirs).status_code, 404)
        self.assertEqual(self.api(self.foreign).get(f"{BASE}/{mine.pk}").status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, "Theirs")

    def test_a_role_of_one_company_never_manages_another_companys_library(self):
        """The foreign manager holds the manage right in THEIR company: it says nothing about ours."""
        template = self.make()
        denied = self.api(self.foreign).patch(f"{BASE}/{template.pk}", {"name": "Taken"}, format="json")
        self.assertEqual(denied.status_code, 404)

    def test_a_user_without_a_company_has_no_templates(self):
        freelancer = _make_user("freelance-tpl@example.com", None)
        self.assertEqual(self.api(freelancer).get(BASE).status_code, 403)


class VariableEngineTests(TemplateBase):
    def test_the_marker_syntax(self):
        self.assertEqual(find_markers("{{ a }} x {{b_2}} {{a}}"), ["a", "b_2"])
        for ordinary in ("{a}", "{{ 1 + 2 }}", "{{}}", "{{ two words }}", '{"k": "v"}', "{{9x}}", "}} {{"):
            self.assertEqual(find_markers(ordinary), [], ordinary)
        self.assertTrue(MARKER_RE.search("{{project_name}}"))

    def test_known_variables_are_replaced_and_spaces_inside_the_braces_are_allowed(self):
        rendered, resolved, unresolved = render_text("A {{ project_name }} B {{project_name}}", {"project_name": "Alpha"})
        self.assertEqual((rendered, resolved, unresolved), ("A Alpha B Alpha", ["project_name"], []))

    def test_a_missing_value_keeps_its_marker_and_is_reported_never_blanked(self):
        rendered, resolved, unresolved = render_text(
            "Hi {{recipient_name}} / {{project_code}} / {{project_name}}", {"project_name": "Alpha", "project_code": "  "},
        )
        self.assertEqual(rendered, "Hi {{recipient_name}} / {{project_code}} / Alpha")
        self.assertEqual(unresolved, ["recipient_name", "project_code"])

    def test_nothing_is_evaluated_or_expanded_twice(self):
        values = {"project_name": "{{recipient_name}} {{ 7*7 }} __import__('os') {0.__class__} <b>x</b>", "recipient_name": "Jane"}
        rendered, _, unresolved = render_text("P: {{project_name}}", values)
        self.assertEqual(rendered, "P: {{recipient_name}} {{ 7*7 }} __import__('os') {0.__class__} <b>x</b>")
        self.assertEqual(unresolved, [])
        # an unknown marker in a text is never looked up in `values` or anywhere else
        rendered, _, unresolved = render_text("{{__class__}} {{ secret }}", {"__class__": "x", "secret": "y"})
        self.assertEqual(rendered, "{{__class__}} {{ secret }}")
        self.assertEqual(unresolved, ["__class__", "secret"])

    def test_values_are_put_on_one_line(self):
        rendered, _, _ = render_text("{{recipient_name}}", {"recipient_name": "Jane\r\nDoe\n"})
        self.assertEqual(rendered, "Jane Doe")


class ApplyTemplateTests(TemplateBase):
    def setUp(self):
        super().setUp()
        grant_project_permission(
            self.owner, self.project, self.company, ["documents.correspondence_create"], "Template Apply Project Role",
        )
        self.project.project_code = "PRJ"
        self.project.save(update_fields=["project_code"])
        self.template = self.make(
            subject_template="{{project_name}} ({{project_code}}) - {{parent_correspondence_reference}}",
            body_template="Hello {{recipient_name}} of {{recipient_company}}.",
        )

    def test_the_variables_are_resolved_from_the_real_sources(self):
        parent = self.create_out_project(subject="Parent")
        response = self.apply(
            self.owner, self.template, project=self.project.id, recipient_contact=self.recipient_contact.id,
            recipient_company=self.client_company.id, parent_correspondence=parent.id,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["subject"], f"Correspondence Test Project (PRJ) - {parent.reference}")
        self.assertEqual(response.data["body"], "Hello Jane Doe of Acme Client.")
        self.assertEqual(response.data["unresolved_variables"], [])
        self.assertFalse(response.data["has_unresolved"])
        self.assertEqual(response.data["language"], "FR")
        self.assertEqual(response.data["context"]["project"], self.project.id)

    def test_the_company_follows_the_contact_when_not_given(self):
        response = self.apply(self.owner, self.template, recipient_contact=self.client_contact.id)
        self.assertEqual(response.data["body"], "Hello Jane Doe of Acme Client.")

    def test_what_the_context_cannot_give_stays_visible_and_is_listed(self):
        response = self.apply(self.owner, self.template)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["subject"], "{{project_name}} ({{project_code}}) - {{parent_correspondence_reference}}")
        self.assertEqual(response.data["body"], "Hello {{recipient_name}} of {{recipient_company}}.")
        self.assertEqual(
            response.data["unresolved_variables"],
            ["project_name", "project_code", "parent_correspondence_reference", "recipient_name", "recipient_company"],
        )
        self.assertTrue(response.data["has_unresolved"])

    def test_an_empty_project_code_is_unresolved_not_blank(self):
        # Project.save() fills a blank code in; a project older than that rule can still have none.
        type(self.project).objects.filter(pk=self.project.pk).update(project_code="")
        response = self.apply(self.owner, self.template, project=self.project.id)
        self.assertIn("{{project_code}}", response.data["subject"])
        self.assertIn("project_code", response.data["unresolved_variables"])

    def test_an_inconsistent_context_is_refused(self):
        other_company = self.client_company.__class__.objects.create(
            company_profile=self.company, company_name="Other Client", company_role="Client", contact_email="o@x.invalid",
        )
        mismatch = self.apply(
            self.owner, self.template, recipient_company=other_company.id, recipient_contact=self.client_contact.id,
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn("recipient_contact", mismatch.data)
        # a parent of another scope (company library) for a project correspondence
        company_parent = self.create_out(subject="Library parent")
        scope = self.apply(self.owner, self.template, project=self.project.id, parent_correspondence=company_parent.id)
        self.assertEqual(scope.status_code, 400)
        self.assertIn("parent_correspondence", scope.data)
        project_parent = self.create_out_project(subject="Project parent")
        no_project = self.apply(self.owner, self.template, parent_correspondence=project_parent.id)
        self.assertEqual(no_project.status_code, 400)

    def test_objects_of_another_company_are_refused_without_revealing_them(self):
        foreign_corr = self.create_out(subject="Mine")
        foreign_user = self.foreign
        foreign_template = self.make(name="Foreign tpl", company=foreign_user.company)
        # the foreign user may apply THEIR template, but not with OUR project / contact / correspondence
        for field, value in (
            ("project", self.project.id), ("recipient_contact", self.client_contact.id),
            ("recipient_company", self.client_company.id), ("parent_correspondence", foreign_corr.id),
        ):
            response = self.apply(foreign_user, foreign_template, **{field: value})
            self.assertEqual(response.status_code, 400, field)
            self.assertEqual(str(response.data[field][0] if isinstance(response.data[field], list) else response.data[field]), "Not found.")

    def test_an_inactive_template_cannot_be_applied(self):
        self.template.is_active = False
        self.template.save()
        self.assertEqual(self.apply(self.owner, self.template).status_code, 400)

    def test_applying_writes_nothing_and_sends_nothing(self):
        corr = self.approved_out(subject="Approved subject", message="Approved message")
        before = {
            "corr": list(Correspondence.objects.order_by("id").values()),
            "events": CorrespondenceSendEvent.objects.count(),
            "workflows": list(CorrespondenceValidationWorkflow.objects.order_by("id").values()),
            "templates": list(CorrespondenceTemplate.objects.order_by("id").values()),
            "history": self.template.history.count(),
        }
        with patch("project_management.services.correspondence_send._deliver_via_provider") as smtp, \
                patch("project_management.services.correspondence_send._deliver_via_graph") as graph:
            response = self.apply(
                self.owner, self.template, project=None, recipient_contact=self.recipient_contact.id,
                parent_correspondence=corr.id if corr.project_id is None else None,
            )
        self.assertEqual(response.status_code, 200, response.content)
        smtp.assert_not_called()
        graph.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(before["corr"], list(Correspondence.objects.order_by("id").values()))
        self.assertEqual(before["events"], CorrespondenceSendEvent.objects.count())
        self.assertEqual(before["workflows"], list(CorrespondenceValidationWorkflow.objects.order_by("id").values()))
        self.assertEqual(before["templates"], list(CorrespondenceTemplate.objects.order_by("id").values()))
        self.assertEqual(before["history"], self.template.history.count())

    def test_a_correspondence_locked_by_its_workflow_is_unaffected_and_still_refuses_edits(self):
        corr = self.approved_out(subject="Locked subject")
        self.apply(self.owner, self.template, recipient_contact=self.recipient_contact.id)
        refused = self.api(self.owner).patch(f"{SEND_BASE}/{corr.pk}", {"subject": "Changed"}, format="json")
        self.assertEqual(refused.status_code, 400)
        corr.refresh_from_db()
        self.assertEqual(corr.subject, "Locked subject")

    def test_a_template_changed_afterwards_never_changes_a_correspondence_already_written(self):
        response = self.apply(self.owner, self.template, project=self.project.id, recipient_contact=self.recipient_contact.id)
        corr = self.create_out_project(subject=response.data["subject"], message=response.data["body"])
        self.template.body_template = "Totally different {{project_name}}"
        self.template.is_active = False
        self.template.save()
        corr.refresh_from_db()
        self.assertEqual(corr.message, "Hello Jane Doe of Acme Client.")


class SendGuardTests(TemplateBase):
    def sent_events(self):
        return CorrespondenceSendEvent.objects.count()

    def test_an_unresolved_placeholder_in_the_message_blocks_the_send_before_anything_happens(self):
        corr = self.approved_out(subject="Fine subject", message="Hello {{recipient_name}}")
        with patch("project_management.services.correspondence_send._deliver_via_provider") as smtp, \
                patch("project_management.services.correspondence_send._deliver_via_graph") as graph:
            with self.assertRaises(SendBlocked) as raised:
                send_correspondence(corr, sender=self.owner)
        smtp.assert_not_called()
        graph.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.sent_events(), 0)
        self.assertIn("body", raised.exception.message_dict)
        self.assertEqual(raised.exception.message_dict["unresolved_variables"], ["recipient_name"])
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)

    def test_the_subject_is_checked_too_and_so_is_an_unknown_lookalike(self):
        corr = self.approved_out(subject="About {{project_name}}", message="Fine")
        with self.assertRaises(SendBlocked) as raised:
            send_correspondence(corr, sender=self.owner)
        self.assertIn("subject", raised.exception.message_dict)
        corr = self.approved_out(subject="Fine", message="x")
        with self.assertRaises(SendBlocked) as raised:
            send_correspondence(corr, sender=self.owner, body="Dear {{ client_name }}")
        self.assertEqual(raised.exception.message_dict["unresolved_variables"], ["client_name"])
        self.assertEqual(self.sent_events(), 0)

    def test_the_check_comes_before_the_mailbox_is_even_validated(self):
        corr = self.approved_out(subject="Fine", message="Hello {{recipient_name}}")
        with self.assertRaises(SendBlocked) as raised:
            send_correspondence(corr, sender=self.owner, mailbox_id=999999)
        self.assertIn("body", raised.exception.message_dict)
        self.assertNotIn("mailbox_id", raised.exception.message_dict)

    def test_an_edited_text_that_no_longer_has_a_placeholder_is_sent_as_before(self):
        corr = self.approved_out(subject="Fine", message="Hello {{recipient_name}}")
        event = send_correspondence(corr, sender=self.owner, body="Hello Jane")
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)
        self.assertEqual(event.body, "Hello Jane")
        self.assertEqual(len(mail.outbox), 1)

    def test_ordinary_text_with_braces_is_not_blocked(self):
        text = 'Config {"a": 1}, set {x}, formula {{ 1 + 2 }}, empty {{}}, price {{9x}}.'
        corr = self.approved_out(subject="Braces {ok}", message=text)
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)
        self.assertEqual(event.body, text)

    def test_a_normal_message_keeps_the_existing_behaviour(self):
        corr = self.approved_out(subject="Original subject", message="Original body")
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual((event.subject, event.body), ("Original subject", "Original body"))
        self.assertEqual(event.to_recipients, [{"email": "jane.doe@links-local.invalid", "name": "Jane Doe"}])
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)

    def test_a_resend_has_the_same_protection(self):
        corr = self.approved_out(subject="Original subject", message="Original body")
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner, body="Again, dear {{recipient_name}}")
        self.assertEqual(self.sent_events(), 1)
        self.assertEqual(len(mail.outbox), 1)
        again = send_correspondence(corr, sender=self.owner, body="Again, dear Jane")
        self.assertEqual(again.result, CorrespondenceSendEvent.Result.SUCCESS)
        self.assertEqual(self.sent_events(), 2)

    def test_the_api_cannot_bypass_the_guard(self):
        corr = self.approved_out(subject="Original subject", message="Original body")
        client = self.api(self.owner)
        blocked = client.post(
            f"{SEND_BASE}/{corr.pk}/send", {"subject": "Hi", "body": "Dear {{recipient_name}}"}, format="json",
        )
        self.assertEqual(blocked.status_code, 400, blocked.content)
        self.assertIn("body", blocked.data)
        self.assertEqual(list(blocked.data["unresolved_variables"]), ["recipient_name"])
        self.assertEqual(self.sent_events(), 0)
        self.assertEqual(len(mail.outbox), 0)
        ok = client.post(f"{SEND_BASE}/{corr.pk}/send", {"subject": "Hi", "body": "Dear Jane"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)

    def test_unresolved_marker_errors_is_empty_for_clean_texts(self):
        self.assertEqual(unresolved_marker_errors(subject="a", body="b {c} {{ 1 }}"), {})
