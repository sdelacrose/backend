from django.core.management.base import BaseCommand
from models.models import GlobalConfig, CompanyConfig
from models.constants import GC_PURCHASE_ORDER_STATUS

class Command(BaseCommand):
    help = 'Add default values for purchase order status to global and company configs'

    def handle(self, *args, **kwargs):
        updated_count = 0
        models = [GlobalConfig, CompanyConfig]

        for model in models:
            configs = model.objects.all()
            for config in configs:
                
                config.purchase_order_status = GC_PURCHASE_ORDER_STATUS
                config.save()
                updated_count += 1

        self.stdout.write(self.style.SUCCESS(f'Successfully updated {updated_count} global and company configuration objects'))
