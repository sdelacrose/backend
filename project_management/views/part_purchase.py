import random
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db.models import Q
from django.db import transaction
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from models.models import SupplierPaymentMethod
from project_management.models import PartPurchaseIntent
from project_management.serializers.part_purchase import (
    PartPurchaseCodeSerializer,
    PartPurchaseIntentSerializer,
    SupplierPaymentMethodSerializer,
)


CODE_EXPIRY_MINUTES = 10
CODE_RESEND_SECONDS = 60
MAX_CODE_ATTEMPTS = 5


def get_available_payment_methods_for_user(user):
    company = getattr(user, "company", None)
    queryset = SupplierPaymentMethod.objects.filter(is_active=True)
    if company:
        return queryset.filter(
            Q(company_profile=company) | Q(company_profile__isnull=True)
        ).select_related("company_profile")
    return queryset.filter(company_profile__isnull=True).select_related("company_profile")


class SupplierPaymentMethodViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = SupplierPaymentMethodSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return SupplierPaymentMethod.objects.none()
        return get_available_payment_methods_for_user(user)


class PartPurchaseIntentViewSet(viewsets.ModelViewSet):
    serializer_class = PartPurchaseIntentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if not user or not user.is_authenticated:
            return PartPurchaseIntent.objects.none()
        queryset = PartPurchaseIntent.objects.filter(buyer=user).select_related(
            "buyer",
            "buyer_company",
            "project",
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)

        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        active_only = self.request.query_params.get("active")
        if active_only in ("1", "true", "True"):
            queryset = queryset.exclude(
                status__in=[
                    PartPurchaseIntent.STATUS_CONFIRMED,
                    PartPurchaseIntent.STATUS_CANCELLED,
                ]
            )
        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(
            buyer=user,
            buyer_company=getattr(user, "company", None),
            status=PartPurchaseIntent.STATUS_DRAFT,
        )

    def _send_code_email(self, intent, code):
        buyer_email = intent.buyer.email
        subject = "Your sharemundo links purchase confirmation code"
        buyer_name = intent.buyer.full_name or buyer_email
        company_name = getattr(intent.buyer_company, "company_name", None)
        message = (
            f"Dear {buyer_name},\n\n"
            f"Your confirmation code for purchase intent {intent.reference} is {code}.\n\n"
            f"This code expires in {CODE_EXPIRY_MINUTES} minutes.\n"
            "This prototype records a purchase intent only. No supplier order "
            "or payment is executed.\n\n"
            "If you did not request this code, you can ignore this email.\n\n"
            "Best regards,\n"
            "sharemundo team"
        )
        html_message = render_to_string(
            "part_purchase_confirmation_code_email.html",
            {
                "buyer_name": buyer_name,
                "company_name": company_name,
                "confirmation_code": code,
                "purchase_reference": intent.reference,
                "expiry_minutes": CODE_EXPIRY_MINUTES,
            },
        )
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [buyer_email],
            html_message=html_message,
            fail_silently=False,
        )

    @action(detail=True, methods=["post"], url_path="send-code")
    def send_code(self, request, pk=None):
        intent = self.get_object()
        if intent.status == PartPurchaseIntent.STATUS_CONFIRMED:
            return Response(
                {"detail": "This purchase intent is already confirmed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not intent.buyer.email:
            return Response(
                {"detail": "The current user does not have an email address."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        if intent.code_sent_at and now < intent.code_sent_at + timedelta(seconds=CODE_RESEND_SECONDS):
            remaining_seconds = int(
                (intent.code_sent_at + timedelta(seconds=CODE_RESEND_SECONDS) - now).total_seconds()
            )
            return Response(
                {
                    "detail": "Please wait before requesting another code.",
                    "retry_after_seconds": remaining_seconds,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        code = f"{random.SystemRandom().randint(0, 999999):06d}"
        intent.confirmation_code_hash = make_password(code)
        intent.code_sent_at = now
        intent.code_expires_at = now + timedelta(minutes=CODE_EXPIRY_MINUTES)
        intent.code_verified_at = None
        intent.code_attempts = 0
        intent.status = PartPurchaseIntent.STATUS_CODE_SENT
        intent.save(
            update_fields=[
                "confirmation_code_hash",
                "code_sent_at",
                "code_expires_at",
                "code_verified_at",
                "code_attempts",
                "status",
                "updated_at",
            ]
        )
        self._send_code_email(intent, code)
        return Response(self.get_serializer(intent).data)

    @action(detail=True, methods=["post"], url_path="verify-code")
    def verify_code(self, request, pk=None):
        intent = self.get_object()
        serializer = PartPurchaseCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not intent.confirmation_code_hash or not intent.code_expires_at:
            return Response(
                {"detail": "No active confirmation code exists for this purchase intent."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if intent.status == PartPurchaseIntent.STATUS_CONFIRMED:
            return Response(
                {"detail": "This purchase intent is already confirmed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timezone.now() > intent.code_expires_at:
            return Response(
                {"detail": "The confirmation code has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if intent.code_attempts >= MAX_CODE_ATTEMPTS:
            return Response(
                {"detail": "Too many failed confirmation attempts. Please request a new code."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not check_password(serializer.validated_data["code"], intent.confirmation_code_hash):
            intent.code_attempts += 1
            intent.save(update_fields=["code_attempts", "updated_at"])
            return Response(
                {"detail": "Invalid confirmation code."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        intent.code_verified_at = timezone.now()
        intent.status = PartPurchaseIntent.STATUS_VERIFIED
        intent.save(update_fields=["code_verified_at", "status", "updated_at"])
        return Response(self.get_serializer(intent).data)

    @action(detail=True, methods=["post"], url_path="confirm")
    @transaction.atomic
    def confirm(self, request, pk=None):
        intent = self.get_object()
        intent = self.get_queryset().select_for_update(of=("self",)).get(pk=intent.pk)
        from project_management.project_access import accessible_projects
        if not accessible_projects(request.user).filter(pk=intent.project_id).exists():
            return Response({"detail": "Project is unavailable."}, status=status.HTTP_403_FORBIDDEN)
        if intent.status == PartPurchaseIntent.STATUS_CONFIRMED:
            return Response(self.get_serializer(intent).data)
        from project_management.services.engineering_approval import validate_purchase_items
        try:
            validate_purchase_items(project=intent.project, items=intent.items, lock=True)
        except ValidationError as exc:
            return Response({"detail": " ".join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
        if not intent.code_verified_at:
            return Response(
                {"detail": "Email confirmation is required before checkout confirmation."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not intent.items:
            return Response(
                {"detail": "At least one cart item is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payment_method_id = (intent.payment_snapshot or {}).get("payment_method_id")
        if not payment_method_id:
            return Response(
                {"detail": "A supplier payment method is required before checkout confirmation."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payment_method = get_available_payment_methods_for_user(
            request.user
        ).filter(id=payment_method_id).first()
        if not payment_method:
            return Response(
                {"detail": "Selected payment method is not available."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payment_snapshot = intent.payment_snapshot or {}
        payment_snapshot.update(
            {
                "payment_method_id": payment_method.id,
                "payment_method_name": payment_method.name,
                "payment_method_type": payment_method.method_type,
                "payment_method_display_details": payment_method.display_details,
                "prototype_only": True,
            }
        )
        intent.payment_snapshot = payment_snapshot

        intent.status = PartPurchaseIntent.STATUS_CONFIRMED
        intent.confirmed_at = timezone.now()
        intent.save(update_fields=["payment_snapshot", "status", "confirmed_at", "updated_at"])
        return Response(self.get_serializer(intent).data)
