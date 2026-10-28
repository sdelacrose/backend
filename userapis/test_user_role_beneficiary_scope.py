# SEC-PERM-01: UserRoleSerializer.validate_user -- the BENEFICIARY of a role assignment must
# belong to the actor's company (a freelancer may only target themselves). The HTTP endpoints are
# read-only (see test_role_endpoints_readonly), so the serializer is exercised directly: it stays
# the second line of defence if a write path is ever reopened.
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from models.models import CompanyProfile, Role, User, UserRole
from userapis.serializers import UserRoleSerializer


class UserRoleBeneficiaryScopeTests(TestCase):
    def setUp(self):
        self.actor = self._user("actor@links-local.invalid", "company_user")
        self.company = self._company(self.actor, "Actor Co")
        self.colleague = self._member("colleague@links-local.invalid", self.company)

        other_admin = self._user("other.admin@links-local.invalid", "company_user")
        self.other_company = self._company(other_admin, "Other Co")
        self.outsider = self._member("outsider@links-local.invalid", self.other_company)

        self.role = Role.objects.create(name="HR full", company=self.company, type="solution", scope="company")
        self.factory = APIRequestFactory()

    def _user(self, email, user_type):
        return User.objects.create_user(
            email=email, password="x", first_name="T", last_name="U", user_type=user_type
        )

    def _company(self, admin, name):
        company = CompanyProfile.objects.create(
            admin_user=admin, company_name=name, company_address="1", post_code="0",
            company_town="T", company_country="C", company_tell_no="0", company_mob_no="1",
            company_web_address="http://x", company_email_address=f"{name[:5].lower()}@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        admin.company = company
        admin.save(update_fields=["company"])
        return company

    def _member(self, email, company):
        user = self._user(email, "company_user")
        user.company = company
        user.save(update_fields=["company"])
        return user

    def _serializer(self, actor, data, instance=None):
        request = self.factory.post("/api/user/user-role/", data, format="json")
        request.user = actor
        return UserRoleSerializer(instance=instance, data=data, context={"request": request}, partial=instance is not None)

    def test_a_colleague_of_the_same_company_is_an_accepted_beneficiary(self):
        serializer = self._serializer(self.actor, {"user": self.colleague.id, "role": self.role.id})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_a_user_of_another_company_is_refused_on_create(self):
        serializer = self._serializer(self.actor, {"user": self.outsider.id, "role": self.role.id})
        self.assertFalse(serializer.is_valid())
        self.assertIn("user", serializer.errors)

    def test_reassigning_an_existing_assignment_to_another_company_user_is_refused(self):
        assignment = UserRole.objects.create(
            user=self.colleague, role=self.role, company=self.company, assigned_by=self.actor
        )
        serializer = self._serializer(self.actor, {"user": self.outsider.id}, instance=assignment)
        self.assertFalse(serializer.is_valid())
        self.assertIn("user", serializer.errors)

    def test_a_freelancer_may_only_target_themselves(self):
        freelancer = self._user("free@links-local.invalid", "freelancer")
        own_role = Role.objects.create(name="Own", freelancer=freelancer, type="solution", scope="company")
        refused = self._serializer(freelancer, {"user": self.colleague.id, "role": own_role.id})
        self.assertFalse(refused.is_valid())
        self.assertIn("user", refused.errors)
        # Beneficiary check passes for self (the role check is a separate, pre-existing rule).
        self_target = self._serializer(freelancer, {"user": freelancer.id, "role": own_role.id})
        self_target.is_valid()
        self.assertNotIn("user", self_target.errors)
