from django.core.management.base import BaseCommand
from aas_integration.models import ProductAASProfile
from aas_integration.services import (
    infer_product_aas_origin,
    synchronize_product_aas_profile,
)


class Command(BaseCommand):
    help = (
        "Report or classify product origins using existing manufacturer and supplier "
        "evidence. The default is a dry run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the reported evidence-based product classifications.",
        )

    def handle(self, *args, **options):
        candidates = list(ProductAASProfile.objects.filter(
            origin=ProductAASProfile.Origin.UNCLASSIFIED,
        ).select_related("product__company_profile"))
        classifications = [
            (profile, infer_product_aas_origin(profile.product, profile))
            for profile in candidates
        ]
        classifications = [
            (profile, origin)
            for profile, origin in classifications
            if origin != ProductAASProfile.Origin.UNCLASSIFIED
        ]
        mode = "apply" if options["apply"] else "dry-run"
        self.stdout.write(
            f"AAS product origin classification ({mode}): "
            f"{len(classifications)} evidence-backed candidate(s)."
        )
        for profile, origin in classifications:
            self.stdout.write(f"Product {profile.product_id}: {origin}")
        if not options["apply"]:
            self.stdout.write("No records changed. Re-run with --apply after review.")
            return

        updated = 0
        for profile, _origin in classifications:
            synchronized = synchronize_product_aas_profile(profile.product)
            updated += int(
                synchronized.origin != ProductAASProfile.Origin.UNCLASSIFIED
            )
        self.stdout.write(self.style.SUCCESS(f"Classified {updated} product origin(s)."))
