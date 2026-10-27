"""CRM-1B: manual follow-up of Network records.

Reuses -- never duplicates -- the existing rules:
* contacts/companies: ContactPermission (`contact_management`) + the tenant filters of
  ContactUserViewSet / ContactCompanyViewSet (freelancers see their own records);
* opportunities: OpportunityViewSet.get_queryset (opportunity_offers view / view_created);
* action items: ActionItemVisibilityService (the Action Items module's own visibility).
"""

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from companyprofileapis.models import Opportunity
from ecpmpapp.utils import require_any_permission_multiple_modules
from project_management.crm_tenancy import tenant_filter_kwargs, tenant_of_user
from project_management.models import ActionItem, ContactCompany, ContactUser, CrmInteraction
from project_management.permissions import ContactPermission
from project_management.services.action_item_queries import ActionItemVisibilityService


def _contact_permission(user, action_name):
    if getattr(user, "is_freelancer", False):
        # Same as ContactUserViewSet/ContactCompanyViewSet: a freelancer manages own records.
        return True
    permission = ContactPermission(action_name, "company")
    checks = {
        "view": permission._check_view_permission,
        "create": lambda u, _o: permission._check_create_permission(u),
        "update": permission._check_update_permission,
        "delete": permission._check_delete_permission,
    }
    return checks[action_name](user, None)


def visible_contact_users(user):
    tenant = tenant_of_user(user)
    if tenant is None or not _contact_permission(user, "view"):
        return ContactUser.objects.none()
    return ContactUser.objects.filter(is_deleted=False, **tenant_filter_kwargs(tenant, owner_field="user"))


def visible_contact_companies(user):
    tenant = tenant_of_user(user)
    if tenant is None or not _contact_permission(user, "view"):
        return ContactCompany.objects.none()
    return ContactCompany.objects.filter(is_deleted=False, **tenant_filter_kwargs(tenant, owner_field="user"))


def visible_opportunities(request):
    from companyprofileapis.views.opportunity import OpportunityViewSet

    view = OpportunityViewSet(request=request, action="list", format_kwarg=None, kwargs={})
    return Opportunity.objects.filter(pk__in=view.get_queryset().values("pk"))


def visible_action_items(request):
    return ActionItemVisibilityService(
        request, permission_checker=require_any_permission_multiple_modules,
    ).resolve().queryset


class CrmInteractionSerializer(serializers.ModelSerializer):
    contact_user = serializers.PrimaryKeyRelatedField(
        queryset=ContactUser.objects.all(), required=False, allow_null=True,
    )
    contact_company = serializers.PrimaryKeyRelatedField(
        queryset=ContactCompany.objects.all(), required=False, allow_null=True,
    )
    opportunity = serializers.PrimaryKeyRelatedField(
        queryset=Opportunity.objects.all(), required=False, allow_null=True,
    )

    class Meta:
        model = CrmInteraction
        fields = (
            "id", "type", "occurred_at", "summary", "author", "contact_user", "contact_company",
            "opportunity", "created_at", "updated_at",
        )
        read_only_fields = ("author", "created_at", "updated_at")

    def validate(self, attrs):
        request = self.context["request"]
        user = request.user
        merged = {
            field: attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
            for field in ("contact_user", "contact_company", "opportunity")
        }
        if not any(merged.values()):
            raise serializers.ValidationError(
                {"links": "An interaction must be linked to a contact, a company or an opportunity."}
            )
        # Every linked record must be one the requester can see in its own module (this also
        # refuses any record of another tenant -- checked server-side, never only in the UI).
        if merged["contact_user"] and not visible_contact_users(user).filter(pk=merged["contact_user"].pk).exists():
            raise serializers.ValidationError({"contact_user": "Contact not found."})
        if merged["contact_company"] and not visible_contact_companies(user).filter(pk=merged["contact_company"].pk).exists():
            raise serializers.ValidationError({"contact_company": "Company not found."})
        if merged["opportunity"] and not visible_opportunities(request).filter(pk=merged["opportunity"].pk).exists():
            raise serializers.ValidationError({"opportunity": "Opportunity not found."})
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        author = instance.author
        data["author_detail"] = (
            {"id": author.id, "name": getattr(author, "full_name", None) or author.email} if author else None
        )
        contact = instance.contact_user
        data["contact_user_detail"] = (
            {"id": contact.id, "name": f"{contact.name} {contact.surname}".strip()} if contact else None
        )
        company = instance.contact_company
        data["contact_company_detail"] = {"id": company.id, "name": company.company_name} if company else None
        opportunity = instance.opportunity
        data["opportunity_detail"] = (
            {"id": opportunity.id, "title": opportunity.title, "reference": opportunity.reference}
            if opportunity else None
        )
        return data


def _scope_filters(params):
    """Interactions/actions/opportunities of ONE record. A company also covers its contacts."""
    contact_id = params.get("contact_user")
    company_id = params.get("contact_company")
    opportunity_id = params.get("opportunity")
    try:
        if contact_id:
            pk = int(contact_id)
            return "contact_user", pk, {
                "interactions": Q(contact_user_id=pk),
                "actions": Q(contact_users=pk) | Q(opportunities__customer_user_id=pk),
                "opportunities": Q(customer_user_id=pk),
            }
        if company_id:
            pk = int(company_id)
            return "contact_company", pk, {
                "interactions": Q(contact_company_id=pk) | Q(contact_user__contact_company_id=pk),
                "actions": (
                    Q(contact_companies=pk)
                    | Q(contact_users__contact_company_id=pk)
                    | Q(opportunities__customer_company_id=pk)
                ),
                "opportunities": Q(customer_company_id=pk) | Q(customer_user__contact_company_id=pk),
            }
        if opportunity_id:
            pk = int(opportunity_id)
            return "opportunity", pk, {
                "interactions": Q(opportunity_id=pk),
                "actions": Q(opportunities=pk),
                "opportunities": Q(pk=pk),
            }
    except (TypeError, ValueError):
        pass
    raise serializers.ValidationError(
        {"scope": "Provide one of contact_user, contact_company or opportunity."}
    )


class CrmInteractionViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = CrmInteractionSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    _ACTION_PERMISSION = {
        "list": "view", "retrieve": "view", "summary": "view",
        "create": "create", "update": "update", "partial_update": "update", "destroy": "delete",
    }

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        needed = self._ACTION_PERMISSION.get(self.action, "view")
        if not _contact_permission(request.user, needed):
            raise PermissionDenied("You do not have permission to manage Network interactions.")

    def get_queryset(self):
        user = self.request.user
        tenant = None if isinstance(user, AnonymousUser) else tenant_of_user(user)
        if tenant is None:
            return CrmInteraction.objects.none()
        queryset = CrmInteraction.objects.filter(**tenant_filter_kwargs(tenant)).select_related(
            "author", "contact_user", "contact_company", "opportunity",
        )
        # An interaction on an opportunity the requester cannot see stays hidden.
        queryset = queryset.filter(
            Q(opportunity__isnull=True) | Q(opportunity__in=visible_opportunities(self.request))
        )
        if self.action == "list" and any(
            self.request.query_params.get(k) for k in ("contact_user", "contact_company", "opportunity")
        ):
            _kind, _pk, filters = _scope_filters(self.request.query_params)
            queryset = queryset.filter(filters["interactions"])
        # One row per interaction, even when it matches through several links.
        return queryset.distinct().order_by("-occurred_at", "-id")

    def perform_create(self, serializer):
        user = self.request.user
        kind, pk = tenant_of_user(user)
        tenant_kwargs = {"company_profile_id": pk} if kind == "company" else {"freelancer_id": pk}
        try:
            serializer.save(author=user, **tenant_kwargs)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)

    def perform_update(self, serializer):
        try:
            serializer.save()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """Synthèse of one contact, company or opportunity: owner, last exchange, next action,
        open/closed follow-ups and linked opportunities -- all within the requester's rights."""
        kind, pk, filters = _scope_filters(request.query_params)
        user = request.user

        owner = None
        if kind == "contact_user":
            record = visible_contact_users(user).select_related("owner").filter(pk=pk).first()
            owner = record.owner if record else None
        elif kind == "contact_company":
            record = visible_contact_companies(user).select_related("owner").filter(pk=pk).first()
            owner = record.owner if record else None
        else:
            record = visible_opportunities(request).select_related("responsible_salesperson").filter(pk=pk).first()
            owner = record.responsible_salesperson if record else None
        if record is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        interactions = self.get_queryset().filter(filters["interactions"])
        last_interaction = interactions.first()

        actions = (
            visible_action_items(request)
            .filter(filters["actions"])
            .distinct()
            .prefetch_related("assignees")
        )
        open_actions = list(actions.filter(closed_at__isnull=True).order_by("target_date", "id")[:50])
        closed_actions = list(actions.filter(closed_at__isnull=False).order_by("-closed_at", "-id")[:20])

        opportunities = (
            visible_opportunities(request)
            .filter(filters["opportunities"])
            .distinct()
            .order_by("-updated_at")
        )

        def person(u):
            return {"id": u.id, "name": getattr(u, "full_name", None) or u.email} if u else None

        def action_row(item):
            return {
                "id": item.id,
                "name": item.name,
                "status": item.status,
                "target_date": item.target_date,
                "closed_at": item.closed_at,
                "company_profile": item.company_profile_id,
                "assignees": [person(a) for a in item.assignees.all()],
            }

        return Response({
            "kind": kind,
            "id": pk,
            "owner": person(owner),
            "last_interaction": (
                CrmInteractionSerializer(last_interaction, context={"request": request}).data
                if last_interaction else None
            ),
            "next_action": action_row(open_actions[0]) if open_actions else None,
            "open_actions": [action_row(a) for a in open_actions],
            "closed_actions": [action_row(a) for a in closed_actions],
            "opportunities": [
                {
                    "id": o.id,
                    "title": o.title,
                    "reference": o.reference,
                    "status": o.status,
                    "sales_phase": o.sales_phase,
                    "value": o.value,
                }
                for o in opportunities[:50]
            ],
            "interactions_count": interactions.count(),
        })
