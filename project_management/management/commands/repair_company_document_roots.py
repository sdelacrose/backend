import json

from django.core.management.base import BaseCommand, CommandError
from project_management.services.company_root_repair import repair_company_roots


class Command(BaseCommand):
    help = 'Merge duplicate company document roots, preserving documents and links. Dry-run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--company-id', action='append', type=int, dest='company_ids')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            repairs = repair_company_roots(options['company_ids'], options['apply'])
        except (ValueError, RecursionError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({'applied': options['apply'], 'repairs': repairs}))
