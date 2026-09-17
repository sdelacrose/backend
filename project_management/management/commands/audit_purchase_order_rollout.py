from django.core.management.base import BaseCommand, CommandError

from project_management.models import PurchaseOrder


class Command(BaseCommand):
    help = (
        "Report Purchase Order rollout data, including rows that cannot be "
        "granted through created-only permissions."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--fail-on-null-created-by",
            action="store_true",
            help="Exit with an error when any PO or Change Notice has no creator.",
        )

    def handle(self, *args, **options):
        queryset = PurchaseOrder.objects.all()
        null_creator_queryset = queryset.filter(created_by__isnull=True)
        null_purchase_order_count = null_creator_queryset.filter(
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        ).count()
        null_change_notice_count = null_creator_queryset.filter(
            document_type=PurchaseOrder.DocumentType.CHANGE_NOTICE,
        ).count()
        null_creator_count = (
            null_purchase_order_count + null_change_notice_count
        )

        self.stdout.write(f"Purchase Order rows: {queryset.count()}")
        self.stdout.write(
            f"Rows with created_by=NULL: {null_creator_count}"
        )
        self.stdout.write(
            f"  Purchase Orders with created_by=NULL: {null_purchase_order_count}"
        )
        self.stdout.write(
            f"  Change Notices with created_by=NULL: {null_change_notice_count}"
        )

        if null_creator_count and options["fail_on_null_created_by"]:
            raise CommandError(
                "Purchase Order rollout audit failed: rows with "
                "created_by=NULL require View or Full Access permissions."
            )

        if null_creator_count:
            self.stdout.write(
                self.style.WARNING(
                    "Created-only permissions will not expose these rows."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "All Purchase Order rows have a creator."
                )
            )
