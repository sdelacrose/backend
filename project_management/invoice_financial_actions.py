"""Invoice card summary and preparation of payment reminders through the existing correspondence flow."""
from decimal import Decimal
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from project_management.currency_converter import ECBCurrencyConverter
from project_management.invoice_financial_summary import financial_invoices, invoice_financial_summary
from project_management.invoice_amounts import get_invoice_total_excl_vat
from project_management.models import CorrespondenceTemplate
from project_management.permissions import CorrespondencePermission
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.services.correspondence_template import (
    apply_template, resolve_context, TemplateContextError, unresolved_marker_errors,
    is_document_distribution_template,
)
from project_management.utils import get_currency_from_record, get_user_currency, symbol_to_currency_code


def annual_invoice_summary(view, request):
    today = timezone.localdate()
    year = today.year
    # The same permission-scoped source as the list; the cards intentionally cover the entire current year.
    invoices = financial_invoices(view.get_queryset(), 'invoiced', today).filter(invoice_date__year=year)
    currency = get_user_currency(request.user)
    target = symbol_to_currency_code(currency).upper()
    converter = ECBCurrencyConverter()
    rates = {}
    def convert(invoice, amount):
        source = symbol_to_currency_code(get_currency_from_record(invoice)).upper()
        if source == target:
            return amount
        if source not in rates:
            rates[source] = converter.get_conversion_rate(source, target)
        rate = rates[source]
        return amount * Decimal(str(rate)) if rate is not None else None
    result = invoice_financial_summary(invoices, today, convert)
    result.update(year=year, company_currency=target, company_currency_symbol='€' if target == 'EUR' else currency)
    return Response(result)


def reminder_context(view, request):
    invoice = view.get_object()
    today = timezone.localdate()
    status = (invoice.status or '').strip().lower()
    if invoice.document_type != 'invoice' or status not in ('open', 'issued', 'due', 'overdue', 'not paid') or not invoice.due_date or invoice.due_date >= today:
        raise ValidationError({'invoice': 'Only issued, unpaid and overdue invoices can be reminded.'})
    user = request.user
    if not getattr(user, 'company_id', None):
        raise PermissionDenied('A company template library is required.')
    project = invoice.project
    scope = 'project' if project else 'company'
    context = {'project': project} if project else {}
    if not CorrespondencePermission()._check_action_or_full_access(user, 'correspondence_create', scope, context):
        raise PermissionDenied()
    if invoice.grand_total is None or invoice.grand_total < 0:
        raise ValidationError({'invoice': 'The invoice amount is missing or invalid.'})
    try:
        template_id = int(request.data.get('template_id'))
    except (ValueError, TypeError):
        raise ValidationError({'template_id': 'Choose a template.'})
    template = get_object_or_404(CorrespondenceTemplate, pk=template_id, company_profile_id=user.company_id, is_active=True)
    if is_document_distribution_template(template.name):
        raise ValidationError({'template_id': 'Choose an e-mail correspondence template.'})
    company_id = invoice.reciepient_company_id
    contact_id = invoice.reciepient_user_id
    if not company_id and not contact_id:
        raise ValidationError({'recipient': 'The invoice has no customer recipient.'})
    try:
        values, ids = resolve_context(user, project_id=invoice.project_id, recipient_company_id=company_id, recipient_contact_id=contact_id)
    except TemplateContextError as error:
        raise ValidationError(error.errors)
    currency = get_currency_from_record(invoice)
    symbol = '€' if symbol_to_currency_code(currency).upper() == 'EUR' else currency
    values.update(
        invoice_reference=invoice.reference_number or str(invoice.id),
        invoice_due_date=invoice.due_date.strftime('%d/%m/%Y'),
        invoice_amount=f'{invoice.grand_total:.2f} {symbol} TTC',
        invoice_amount_excl_vat=f'{get_invoice_total_excl_vat(invoice):.2f} {symbol} HT',
    )
    return invoice, template, values, ids


def invoice_reminder_preview(view, request):
    invoice, template, values, ids = reminder_context(view, request)
    result = apply_template(template, values)
    result.update(template_id=template.id, invoice_id=invoice.id)
    return Response(result)


def prepare_invoice_reminder(view, request):
    invoice, template, values, ids = reminder_context(view, request)
    rendered = apply_template(template, values)
    subject = request.data.get('subject', rendered['subject'])
    body = request.data.get('body', rendered['body'])
    if not isinstance(subject, str) or not isinstance(body, str) or not subject.strip() or not body.strip() or len(subject) > 500 or len(body) > 20000:
        raise ValidationError({'message': 'Enter a subject and a message within the template limits.'})
    unresolved = unresolved_marker_errors(subject=subject, body=body)
    if unresolved:
        raise ValidationError(unresolved)
    payload = {
        'direction': 'OUT', 'status': 'DRAFT', 'subject': subject, 'message': body,
        'external_reference': invoice.reference_number or str(invoice.id),
        'issue_date': timezone.localdate(),
        'recipient_company': ids['recipient_company'], 'recipient_contact': ids['recipient_contact'],
    }
    if invoice.project_id:
        payload['project'] = invoice.project_id
    serializer = CorrespondenceSerializer(data=payload, context={'request': request})
    serializer.is_valid(raise_exception=True)
    correspondence = serializer.save()
    # Preparing a reminder never sends e-mail. Existing validation, mailbox, send and evidence controls apply.
    return Response({'correspondence_id': correspondence.id, 'project_id': correspondence.project_id}, status=201)
