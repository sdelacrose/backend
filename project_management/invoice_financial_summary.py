"""Shared rules for annual invoice cards and their list filters (net amounts)."""
from decimal import Decimal, InvalidOperation
from django.db.models.functions import Lower, Trim
from project_management.invoice_amounts import get_invoice_total_excl_vat, money

EMITTED_STATUSES = ('open', 'issued', 'due', 'overdue', 'paid', 'not paid')
BUCKETS = ('invoiced', 'paid', 'current', 'overdue')


def financial_invoices(queryset, bucket, today):
    queryset = queryset.filter(document_type='invoice').annotate(
        financial_status=Lower(Trim('status')),
    ).filter(financial_status__in=EMITTED_STATUSES)
    if bucket == 'paid':
        return queryset.filter(financial_status='paid')
    if bucket == 'current':
        return queryset.exclude(financial_status='paid').filter(due_date__gte=today)
    if bucket == 'overdue':
        return queryset.exclude(financial_status='paid').filter(due_date__lt=today)
    return queryset


def invoice_financial_summary(invoices, today, convert):
    amounts = {key: Decimal('0') for key in BUCKETS}
    count = 0
    complete = True
    for invoice in invoices:
        status = (invoice.status or '').strip().lower()
        if invoice.document_type != 'invoice' or status not in EMITTED_STATUSES:
            continue
        count += 1
        try:
            net = get_invoice_total_excl_vat(invoice) if invoice.grand_total is not None else None
            net = convert(invoice, net) if net is not None else None
            if net is None or not Decimal(str(net)).is_finite() or net < 0:
                complete = False
                continue
            net = money(net)
        except (InvalidOperation, ZeroDivisionError, ValueError, TypeError):
            complete = False
            continue
        amounts['invoiced'] += net
        if status == 'paid':
            amounts['paid'] += net
        elif invoice.due_date is None:
            complete = False
        elif invoice.due_date < today:
            amounts['overdue'] += net
        else:
            amounts['current'] += net
    return {
        'invoice_count': count,
        'summary_complete': complete,
        **{key + '_amount_excl_vat': float(money(value)) if complete else None for key, value in amounts.items()},
    }
