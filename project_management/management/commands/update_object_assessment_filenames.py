import os
from django.core.management.base import BaseCommand
from project_management.models import ObjectAssessment


class Command(BaseCommand):
    help = 'Updates file_name field for existing ObjectAssessment objects by extracting it from the file URL'

    def handle(self, *args, **options):
        # Get all object assessments
        object_assessments = ObjectAssessment.objects.all()
        total_count = object_assessments.count()
        updated_count = 0
        skipped_count = 0
        error_count = 0

        self.stdout.write(self.style.SUCCESS(f'Found {total_count} object assessments to process'))

        # Process each object assessment
        for assessment in object_assessments:
            try:
                # Skip if file_name is already set
                if assessment.file_name:
                    skipped_count += 1
                    continue

                # Skip if no file is associated
                if not assessment.file:
                    skipped_count += 1
                    continue

                # Extract filename from file URL
                file_url = assessment.file.name
                if file_url:
                    # Get the filename from the path
                    filename = os.path.basename(file_url)
                    
                    # Update the object assessment
                    assessment.file_name = filename
                    assessment.save(update_fields=['file_name'])
                    updated_count += 1
                    
                    self.stdout.write(f'Updated ObjectAssessment ID {assessment.id} with filename: {filename}')
                else:
                    skipped_count += 1
            except Exception as e:
                error_count += 1
                self.stdout.write(self.style.ERROR(f'Error processing ObjectAssessment ID {assessment.id}: {str(e)}'))

        # Print summary
        self.stdout.write(self.style.SUCCESS(f'Completed processing {total_count} object assessments'))
        self.stdout.write(self.style.SUCCESS(f'Updated: {updated_count}'))
        self.stdout.write(self.style.SUCCESS(f'Skipped (already had filename or no file): {skipped_count}'))
        self.stdout.write(self.style.SUCCESS(f'Errors: {error_count}'))