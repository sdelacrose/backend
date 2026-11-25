"""Bounded spreadsheet intake. Formula cells and unknown columns are never executed."""
import csv
from io import BytesIO, StringIO
from zipfile import ZipFile, BadZipFile
from django.core.exceptions import ValidationError
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.workbook.defined_name import DefinedName
from supplier_portal.register import FIELDS, MAX_ROWS

VISIBLE_FIELDS = ('file_name', 'identification_number', 'document_type', 'discipline', 'supplier_transmittal_category', 'description')
HEADERS = {
    'en': ['Document title', 'Supplier reference', 'Document type', 'Discipline', 'Transmission purpose', 'Description'],
    'fr': ['Titre du document', 'Référence fournisseur', 'Type documentaire', 'Discipline', 'Finalité de transmission', 'Description'],
    'de': ['Dokumenttitel', 'Lieferantenreferenz', 'Dokumenttyp', 'Fachgebiet', 'Übermittlungszweck', 'Beschreibung'],
}


class ImportedRows(list):
    def __init__(self):
        super().__init__()
        self.line_numbers = []


def template_bytes(options=None, language='en'):
    language = str(language).lower().split('-')[0]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Documents'
    sheet.append(HEADERS.get(language, HEADERS['en']))
    sheet.freeze_panes = 'A2'
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width = 30
    notes = workbook.create_sheet('Instructions')
    instructions = [
        'One document per row. Do not rename columns. Maximum 1000 rows.',
        'Document title is required. No physical file or revision is created by this import.',
        'Project, supplier and destination are imposed by the authorized project context.',
        'Other columns follow the existing document form and the project configuration.',
        'Document type and Discipline: copy the existing labels from the Project choices sheet.',
        'Transmission purpose: FOR_REVIEW or FOR_INFORMATION, or empty when unspecified.',
        'Do not put an external revision in this list: the revision belongs to the later physical submission.',
        'Invalid or duplicate rows are refused. Correct and resubmit them in a new batch.',
        'Valid proposals await company acceptance before any physical upload.',
    ]
    if language == 'fr':
        instructions = ['Une ligne par document, au maximum 1000 lignes. Conservez les intitulés de colonnes.',
            'Le titre est obligatoire. Cet import ne crée aucun fichier physique ni aucune révision.',
            'Le projet, le fournisseur et le dossier sont déterminés par votre accès au projet.',
            'Les autres métadonnées suivent les mêmes contrôles que le formulaire de création documentaire.',
            'Choisissez les types et disciplines du projet dans les listes déroulantes ou la feuille Project choices.',
            'Finalité : Pour revue ou Pour information. Laissez vide si elle n’est pas précisée.',
            'L’indice de révision sera saisi lors du dépôt physique, pas dans cette liste prévisionnelle.',
            'Les doublons et lignes invalides seront refusés. Corrigez-les et soumettez un nouveau lot.',
            'Les propositions conformes attendent l’acceptation de l’entreprise avant de pouvoir recevoir un fichier.']
    elif language == 'de':
        instructions = ['Eine Zeile pro Dokument, höchstens 1000 Zeilen. Spaltenüberschriften beibehalten.',
            'Der Titel ist erforderlich. Dieser Import erstellt weder Dateien noch Revisionen.',
            'Projekt, Lieferant und Zielordner werden durch Ihre Projektberechtigung bestimmt.',
            'Andere Metadaten unterliegen denselben Prüfungen wie im Dokumentformular.',
            'Wählen Sie Projekttypen und Fachgebiete aus den Listen oder dem Blatt Project choices.',
            'Zweck: Zur Prüfung oder Zur Information. Leer lassen, falls nicht angegeben.',
            'Die Revisionskennung wird erst beim Datei-Upload angegeben.',
            'Doppelte und ungültige Zeilen werden abgelehnt. Korrigieren und erneut einreichen.',
            'Gültige Vorschläge benötigen vor dem Datei-Upload die Annahme durch das Unternehmen.']
    for text in instructions:
        notes.append([text])
    notes.column_dimensions['A'].width = 120
    if options:
        choices = workbook.create_sheet('Project choices')
        choices.append(['Document type', 'Discipline'])
        for label in options['document_types']:
            choices.append([label])
        for discipline in options['disciplines']:
            choices.append(['', discipline.get('display_label') or discipline['label']])
        for row in choices:
            for cell in row:
                cell.data_type = 's'
        ranges = []
        if options['document_types']:
            ranges.append(('SupplierDocumentTypes', f"'Project choices'!$A$2:$A${len(options['document_types']) + 1}", 'C2:C1001'))
        if options['disciplines']:
            start = len(options['document_types']) + 2
            ranges.append(('SupplierDisciplines', f"'Project choices'!$B${start}:$B${start + len(options['disciplines']) - 1}", 'D2:D1001'))
        for name, reference, cells in ranges:
            item = DefinedName(name, attr_text=reference)
            if hasattr(workbook.defined_names, 'add'):
                workbook.defined_names.add(item)
            else:
                workbook.defined_names.append(item)
            validation = DataValidation(type='list', formula1=name, allow_blank=True)
            validation.showErrorMessage = True
            sheet.add_data_validation(validation)
            validation.add(cells)
    purposes = {'fr': 'Pour revue,Pour information', 'de': 'Zur Prüfung,Zur Information'}
    purpose = DataValidation(type='list', formula1='"' + purposes.get(language, 'For review,For information') + '"', allow_blank=True)
    sheet.add_data_validation(purpose)
    purpose.add('E2:E1001')
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def parse_list(upload):
    if upload.size > 5 * 1024 * 1024:
        raise ValidationError('The metadata import exceeds 5 MiB.')
    data = upload.read()
    name = upload.name.lower()
    try:
        if name.endswith('.xlsx'):
            with ZipFile(BytesIO(data)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 25 * 1024 * 1024:
                    raise ValidationError('The expanded workbook is too large.')
            workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
            try:
                sheet = workbook['Documents'] if 'Documents' in workbook.sheetnames else workbook.worksheets[0]
                if (sheet.max_column or 0) > len(FIELDS) or (sheet.max_row or 0) > MAX_ROWS + 1:
                    raise ValidationError('Too many rows or columns in this workbook.')
                values = []
                for line, cells in enumerate(sheet.iter_rows(max_row=MAX_ROWS + 2, max_col=min(sheet.max_column or len(FIELDS), len(FIELDS) + 1)), 1):
                    if line > (sheet.max_row or 0):
                        break
                    if line > MAX_ROWS + 1 or len(cells) > len(FIELDS):
                        raise ValidationError('Too many rows or columns in this workbook.')
                    if any(cell.data_type == 'f' for cell in cells):
                        raise ValidationError('Formula cells are not allowed in a document list.')
                    values.append([str(cell.value).strip() if cell.value is not None else '' for cell in cells])
            finally:
                workbook.close()
        elif name.endswith('.csv'):
            text = data.decode('utf-8-sig')
            delimiter = ';' if text.splitlines()[0].count(';') > text.splitlines()[0].count(',') else ','
            values = []
            for row in csv.reader(StringIO(text), delimiter=delimiter):
                if len(values) >= MAX_ROWS + 1:
                    raise ValidationError('The import exceeds 1000 rows.')
                values.append(row)
        else:
            raise ValidationError('Use an XLSX or UTF-8 CSV document list.')
    except ValidationError:
        raise
    except Exception as error:
        raise ValidationError('The document list cannot be read.') from error
    if not values:
        raise ValidationError('The document list is empty.')
    aliases = {label.casefold(): field for labels in HEADERS.values() for label, field in zip(labels, VISIBLE_FIELDS)}
    headers = [aliases.get(str(value or '').strip().casefold(), str(value or '').strip()) for value in values[0]]
    if len(headers) != len(set(headers)) or not headers or any(key not in FIELDS for key in headers):
        raise ValidationError('Use distinct column names from the supplied template.')
    rows = ImportedRows()
    for number, cells in enumerate(values[1:], 2):
        if not any(str(c).strip() for c in cells):
            continue
        if len(cells) > len(headers):
            raise ValidationError('A row contains more values than the header.')
        rows.append(dict(zip(headers, list(cells) + [''] * (len(headers) - len(cells)))))
        rows.line_numbers.append(number)
    if not rows:
        raise ValidationError('The document list has no data rows.')
    return rows
