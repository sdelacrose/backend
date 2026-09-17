from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ecpmpapp.constants import StatusType
from ecpmpapp.utils import require_any_permission_multiple_modules
from human_resource_management.models import LeaveRequest
from models.constants import FeedbackStatusType
from models.models import Feedback
from project_management.models import ExpenseCard, MonthlyTimesheetSubmission
from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
from project_management.utils import get_product_name_from_request


class ActionCountersView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        user = request.user
        if isinstance(user, AnonymousUser):
            return Response(self._build_response(0, 0, 0))

        timesheets_count = self._get_timesheets_count(user)
        leaves_count = self._get_leaves_count(user)
        expenses_count = self._get_expenses_count(user)
        feedback_count = self._get_feedback_count(user)

        return Response(
            self._build_response(
                timesheets_count=timesheets_count,
                leaves_count=leaves_count,
                expenses_count=expenses_count,
                feedback_count=feedback_count,
            )
        )

    def _build_response(
        self,
        timesheets_count,
        leaves_count,
        expenses_count,
        feedback_count,
    ):
        time_booking_total = timesheets_count + leaves_count

        return {
            "sidebar": {
                "time_booking": time_booking_total,
                "cost_management": expenses_count,
                "feedback": feedback_count,
            },
            "sections": {
                "time_booking": {
                    "total": time_booking_total,
                    "tabs": {
                        "timesheets": timesheets_count,
                        "leaves": leaves_count,
                    },
                },
                "cost_management": {
                    "total": expenses_count,
                    "tabs": {
                        "expenses_report": expenses_count,
                    },
                },
                "feedback": {
                    "total": feedback_count,
                    "tabs": {
                        "feedback": feedback_count,
                    },
                },
            },
        }

    def _has_company_permission(self, user, modules_actions):
        return require_any_permission_multiple_modules(
            user,
            modules_actions,
            "company",
            {},
        )

    def _get_timesheets_count(self, user):
        if not self._has_company_permission(
            user,
            {"time_booking": ["view", "review", "full_access"]},
        ):
            return 0

        if user.is_freelancer or not user.company_id:
            return 0

        return MonthlyTimesheetSubmission.objects.filter(
            user__company_id=user.company_id,
            status=StatusType.PENDING.value,
        ).count()

    def _get_leaves_count(self, user):
        if not self._has_company_permission(
            user,
            {"leave_request": ["review"]},
        ):
            return 0

        if user.is_freelancer or not user.company_id:
            return 0

        return LeaveRequest.objects.filter(
            employee__user__company_id=user.company_id,
            status=StatusType.PENDING.value,
        ).exclude(employee__user=user).distinct().count()

    def _get_expenses_count(self, user):
        if not self._has_company_permission(
            user,
            {"expense_booking": ["view", "review", "full_access"]},
        ):
            return 0

        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        else:
            products = [product_name]

        if not user.is_freelancer and user.company_id:
            expense_filters = (
                (
                    Q(project__isnull=False, project__solution__in=products)
                    & (
                        Q(project__user__company=user.company)
                        | Q(project__team_members__team_member=user)
                    )
                )
                | Q(company_profile__isnull=False, company_profile=user.company)
            )
        elif user.is_freelancer:
            expense_filters = (
                (
                    Q(project__isnull=False, project__solution__in=products)
                    & Q(project__team_members__team_member=user)
                )
                | Q(freelancer__isnull=False, freelancer=user)
            )
        else:
            return 0

        return ExpenseCard.objects.filter(
            expense_filters,
            status=StatusType.PENDING.value,
        ).distinct().count()

    def _get_feedback_count(self, user):
        return Feedback.objects.filter(
            user=user,
            status=FeedbackStatusType.OPEN.value,
        ).count()
