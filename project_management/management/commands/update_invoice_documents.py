from django.core.management.base import BaseCommand
from project_management.models import Document
from models.constants import GC_DOCUMENT_STATUSES

class Command(BaseCommand):
    help = 'Updates status and document_type for all existing invoice documents based on document_type'

    def add_arguments(self, parser):
        parser.add_argument(
            '--status',
            default='Draft',
            help='Status to set for invoice documents (default: Draft)'
        )
        parser.add_argument(
            '--type',
            default='Customer Invoice',
            help='Document type to set for invoice documents (default: Customer Invoice)'
        )

    def handle(self, *args, **options):
        status = options['status']
        new_doc_type = options['type']
        
        # Find all documents that appear to be invoices based on document_type
        invoice_docs = Document.objects.filter(document_type__in=["Customer invoice", "Custome invoice", "Customer Invoice"])
        
        total_count = invoice_docs.count()
        updated_status_count = 0
        updated_type_count = 0
        
        self.stdout.write(self.style.SUCCESS(f'Found {total_count} invoice documents to process'))
        
        for doc in invoice_docs:
            try:
                changes = []
                
                # Always update status
                if doc.status not in GC_DOCUMENT_STATUSES and doc.status != status:
                    old_status = doc.status
                    doc.status = status
                    updated_status_count += 1
                    changes.append(f'Status: {old_status} → {status}')
                
                # Only update type if current type is "Customer Invoice" or "Cutome Invoice"
                if doc.document_type in ["Customer invoice", "Custome invoice"] and doc.document_type != new_doc_type:
                    old_type = doc.document_type
                    doc.document_type = new_doc_type
                    updated_type_count += 1
                    changes.append(f'Type: {old_type} → {new_doc_type}')
                
                if changes:
                    doc.save()
                    self.stdout.write(f'Updated document ID {doc.id}: {", ".join(changes)}')
                
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f'Error updating document ID {doc.id}: {str(e)}')
                )
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully updated {updated_status_count} document statuses and '
                f'{updated_type_count} document types out of {total_count} invoice documents'
            )
        )