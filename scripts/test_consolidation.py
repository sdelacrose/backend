"""Focused cross-module release gate on a disposable DB; outbound providers forbidden."""
import django
from unittest.mock import patch

django.setup()
from django.db import connection
from django.core.management import call_command
from django.test import override_settings
assert connection.settings_dict['NAME'] in {'links_recette_ci','links_recette_update_20261124'}
modules = [
 'ecpmpapp.test_license_permission','ecpmpapp.test_websocket_jwt','userapis.test_recette_guard',
 'project_management.test_contact_tenant_isolation','userapis.test_role_endpoints_readonly','userapis.test_user_role_beneficiary_scope',
 *['project_management.test_quality_q'+str(n) for n in range(1,8)],
 'project_management.test_invoice_financial_actions','project_management.test_invoice_financial_summary',
 'project_management.test_po_linked_invoice_amounts','project_management.test_purchase_order_end_date',
 'project_management.test_purchase_order_summary','project_management.test_register_date_filters',
 'project_management.test_register_response_filters','project_management.test_template_activation',
 'project_management.test_document_client_due_date','project_management.test_document_register_control_status',
 'project_management.test_intervention_kpi_cards','project_management.test_intervention_kpis',
]
with patch('requests.sessions.Session.request', side_effect=AssertionError('Unmocked outbound HTTP forbidden')), patch('smtplib.SMTP.connect', side_effect=AssertionError('Real SMTP forbidden')):
 call_command('check')
 call_command('makemigrations', check=True, dry_run=True, verbosity=1)
 call_command('test', *modules, verbosity=1, interactive=False, keepdb=True)
 # Existing generic mail-fixture tests simulate SMTP and explicitly mock transport.
 # The dedicated recette-policy tests above exercise the live recette restrictions.
 with override_settings(RECETTE_MODE=False,EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
  call_command('test','project_management.test_document_download_package','project_management.test_correspondence_send_proof_not_started',verbosity=1,interactive=False,keepdb=True)
