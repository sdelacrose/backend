from enum import Enum
import re
class UserType(Enum):
    USER = 'user'
    INTERNAL_USER = 'internal_user'
    COMPANY_USER = 'company_user'
    ADMIN = 'admin'
    FREELANCER = 'freelancer'
    COMPANY = 'company'
    PROJECT_MANAGER = 'project manager'
    COMPANY_OWNER = 'company_owner'
    COMPANY_ADMIN = "company_admin"

class StatusType(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    PAID = "paid"
    OPEN = "open"
    CONVERTED_TO_PROJECT = "converted_to_project"
    CONVERTED = "converted"

class LeaveType(Enum):
    WORK_FROM_HOME = "Work From Home"
    LEAVE = "Leave"
    OTHER = "Other"
    SICK = "Sick"
    UNPAID = "Unpaid"

class ChatStatusType(Enum):
    DELIVERED = "delivered"
    READ = "read"


ENTERPRISE_SOLUTION = "Enterprise Solution"
ENERGY_CONSULTANT_SOLUTION = "Energy Consultant Solution"
CORE_SOLUTION = "Core Solution"

# Template strings for document upload paths:
UPLOAD_PATH_PROJECT      = "documents/project_{project_id}/folder_{folder_id}/{filename}"
UPLOAD_PATH_COMPANY      = "documents/company_{company_profile_id}/folder_{folder_id}/{filename}"
UPLOAD_PATH_FREELANCER   = "documents/freelancer_{freelancer_id}/folder_{folder_id}/{filename}"

# New template for object assessment uploads:
UPLOAD_PATH_ASSESSMENT   = "object_assessment/project_{project_id}/{record_type}/{filename}"

# L3G-1: Supplier Surveillance final evidence (never a Document/Folder -- the evidence PDF
# must never appear in Project -> Documents -> Livrables, see project_management.models.
# supplier_surveillance_evidence).
UPLOAD_PATH_SUPPLIER_SURVEILLANCE_EVIDENCE = (
    "supplier_surveillance_evidence/project_{project_id}/{surveillance_reference}/{filename}"
)

S3_URL_TEMPLATE        = "https://{bucket}.s3.{region}.amazonaws.com/{file_name}"

MS_OAUTH_AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MS_OAUTH_TOKEN_URL     = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_OUTLOOK_CALENDAR_SCOPES = "Calendars.ReadWrite Contacts.Read offline_access"
MS_GRAPH_SITES_SEARCH_URL = "https://graph.microsoft.com/v1.0/sites/?search=*"
MS_GRAPH_DRIVE_CHILDREN_URL     = "https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/children"
MS_OAUTH_TENANT_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

# Microsoft Graph: Get children of a specific drive item
MS_GRAPH_ITEM_CHILDREN_URL = "https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{item_id}/children"
MS_GRAPH_DRIVE_ITEM_CONTENT_URL = "https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{item_id}/content"
PLACEHOLDER_LOGO_URL = "https://placehold.co/250x150?text=LOGO"
# Generic graphic for purchase order PDFs (not the company profile logo).
PO_PDF_LOGO_URL = "https://placehold.co/220x72/png?text=PO"

MS_GRAPH_USER_CALENDAR_EVENTS_URL = "https://graph.microsoft.com/v1.0/me/calendar/events"
MS_GRAPH_USER_CONTACTS_URL = "https://graph.microsoft.com/v1.0/me/contacts"

# MAIL-1.1C2: Mail scopes requested on TOP of the existing Calendar/Contacts scopes (never
# instead of them) -- a token issued for the Mail-connect flow must still work for Calendar/
# Contacts (point 2 of the ticket's own pre-check: "comment demander un consentement
# supplémentaire sans casser Calendar/Contacts"), since UserOutlookCredentials holds exactly
# one access/refresh token pair per user, shared by every Microsoft Graph feature. Mail.Send/
# Mail.ReadWrite cover an individual mailbox; the .Shared variants are what a delegate's own
# token needs to act on a shared mailbox (still subject to real Exchange-side Send As/Send on
# Behalf/Full Access configuration this code can never grant or fully verify -- see
# verify_shared_mailbox_access in userapis/mailbox_oauth.py).
# MAIL-RECETTE-01J: User.Read is the least delegated permission Microsoft Graph documents for
# GET /organization (the recette tenant check) and GET /me (the sender identity probe). Entra's
# "API permissions" only lets the app ask for it: a v2 token carries just the scopes REQUESTED here.
MS_MAIL_SCOPES = (
    MS_OUTLOOK_CALENDAR_SCOPES
    + " Mail.Send Mail.ReadWrite Mail.Send.Shared Mail.ReadWrite.Shared User.Read"
)
# A deliberately minimal, read-only probe of actual mailbox access (never sends/moves/deletes
# anything) -- used only to decide whether a SHARED mailbox connection may transition to
# "active" (section 5: OAuth success alone is never treated as proof of real Exchange rights).
MS_GRAPH_USER_MAILFOLDER_URL = "https://graph.microsoft.com/v1.0/users/{email}/mailFolders/inbox"
# MAIL-1.2A: {email} is always the MailboxConnection's own address, for both an individual
# mailbox (the token's own identity -- Graph treats this exactly like /me/sendMail) and a
# shared mailbox (the delegate's token, Graph itself decides whether Send As/Send on Behalf
# actually applies -- see userapis/mailbox_send.py). Never a second, different sender.
MS_GRAPH_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/users/{email}/sendMail"
# MAIL-1.2A-REVIEW section 2: a minimal, read-only identity probe -- confirms the Microsoft
# account actually behind an access token before it is trusted to send as an INDIVIDUAL
# MailboxConnection's own claimed email_address (nothing before this enforced that the two
# ever matched; see _verify_individual_sender_identity in userapis/mailbox_send.py).
MS_GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName"
# MAIL-1.3B: base of the READ-ONLY message requests (userapis/mailbox_read.py): "/me" for an
# individual mailbox, "/users/{email}" for a shared one. Only GET is ever issued against it.
MS_GRAPH_MAIL_BASE_URL = "https://graph.microsoft.com/v1.0"
VALID_REMINDER_PATTERN = re.compile(r"^\d+\s?(day|days|week|weeks|month|months)$",re.IGNORECASE)

CUSTOM_LABELS_TIMECARD = {
    'freelancer':         'Freelancer',
    'project':            'Project',
    'company_profile':    'Company Profile',
    'name':               'Name',
    'date':               'Date',
    'description':        'Description',
    'hours':              'Hours',
    'milestone':          'Milestone',
    'action_item':        'Action Item',
    'status':             'Status',
    'rejection_reason':   'Rejection Reason',
    'approved_by':        'Approved By',
    'rejected_by':        'Rejected By',
    'created_by':         'Created By',
    'created_at':         'Created At',
    'updated_at':         'Updated At',
}

CUSTOM_LABELS_EXPENSECARD = {
    'project':            'Project',
    'company_profile':    'Company Profile',
    'freelancer':         'Freelancer',
    'name':               'Name',
    'description':        'Description',
    'category':           'Category',
    'milestone':          'Milestone',
    'action_item':        'Action Item',
    'rate':               'Rate',
    'quantity':           'Quantity',
    'total':              'Total',
    'expense_date':       'Expense Date',
    'status':             'Status',
    'rejection_reason':   'Rejection Reason',
    'approved_by':        'Approved By',
    'rejected_by':        'Rejected By',
    'created_by':         'Created By',
    'created_at':         'Created At',
    'updated_at':         'Updated At',
}


# any custom overrides you like
CUSTOM_LABELS_PROJECTS = {
    'solution': 'Solution',
    'project_name': 'Project Name',
    'project_description': 'Description',
    'funding_strategy': 'Funding Strategy',
    'lat_long': 'Latitude/Longitude',
    'contract_type': 'Contract Type',
    'international_standard': 'International Standard',
    'major_project_model': 'Major Project Model',
    'commercial_model': 'Commercial Model',
    'supporting_agreement': 'Supporting Agreement',
    'contract_value': 'Contract Value',
    'address': 'Address',
    'town': 'Town/City',
    'country': 'Country',
    'currency': 'Currency',
    'contract_details': 'Contract Details',
    'postcode': 'Postcode',
    'project_image': 'Project Image',
    'project_priority': 'Priority',
    'project_start_date': 'Start Date',
    'project_end_date': 'End Date',
    'project_status': 'Status',
    'project_type': 'Type',
    'project_colour': 'Colour',
    'place_id': 'Place ID',
    'type_of_service': 'Service Type',
    'company_profile': 'Company Profile',
    'user': 'Owner',
    'created_by': 'Created By',
    'created_by_email': 'Creator Email',
    'created_by_name': 'Creator Name',
    'sharepoint_site_id': 'SharePoint Site ID',
    'sharepoint_site_name': 'SharePoint Site Name',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}

CUSTOM_TEAM_MEMBER_LABELS = {
    'role': 'Role',
    'project_access': 'Access Level',
    'team_member': 'Team Member',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}

CUSTOM_LABELS_ACTION_ITEMS = {
    'milestone': 'Milestone',
    'parent_action_item': 'Parent Action Item',
    'assignee': 'Assignee',
    'company_profile': 'Company Profile',
    'freelancer': 'Freelancer',
    'name': 'Name',
    'notes': 'Notes',
    'allocated_time': 'Allocated Time',
    'start_date': 'Start Date',
    'target_date': 'Target Date',
    'reminder': 'Reminder',
    'status': 'Status',
    'allocated_budget': 'Allocated Budget',
    'priority_level': 'Priority Level',
    'closed_at': 'Closed At',
    'closed_by': 'Closed By',
    'created_by': 'Created By',
    'documents': 'Documents',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}


CUSTOM_LABELS_MILESTONE = {
    'name': 'Milestone Name',
    'description': 'Description',
    'colour': 'Colour',
    'status': 'Status',
    'reminder_setting': 'Reminder Setting',
    'allocated_budget': 'Allocated Budget',
    'allocated_time': 'Allocated Time',
    'phase_title': 'Phase Title',
    'start_date': 'Start Date',
    'due_date': 'Due Date',
    'closed_at': 'Closed At',
    'closed_by': 'Closed By',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}

CUSTOM_LABELS_OBJECT_ASSESSMENT = {
    'record_type': 'Record Type',
    'record_title': 'Record Title',
    'file': 'File',
    'description': 'Description',
    'author': 'Author',
    'document_type': 'Document Type',
    'identification_number': 'Identification Number',
    'status': 'Status',
    'file_name': 'File Name',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}

CUSTOM_LABELS_PROJECT_PHASE = {
    'name': 'Phase Name',
    'date': 'Date',
    'created_by': 'Created By',
    'status': 'Status',
    'amount': 'Amount',
    'purchase_orders': 'Purchase Orders',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}

CUSTOM_LABELS_PROJECT_FUNDING = {
    'name': 'Funding Name',
    'budget': 'Budget',
    'created_by': 'Created By',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}

CUSTOM_LABELS_PURCHASE_ORDER = {
    'po_number': 'PO Number',
    'budget': 'Budget',
    'attachments': 'Attachments',
    'start_date': 'Start Date',
    'end_date': 'End Date',
    'progress': 'Progress',
    'status': 'Status',
    'created_by': 'Created By',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}


CUSTOM_LABELS_DOCUMENT = {
    'file_name':            'File Name',
    'document_type':        'Document Type',
    'identification_number':'Identification Number',
    'status':               'Status',
    'category':             'Category',
    'discipline':           'Discipline',
    'sequence_number':      'Sequence Number',
    'revision':             'Revision',
    'full_document_number': 'Document Number',
    'uploaded_at':          'Uploaded At',
    'updated_at':           'Updated At',
}

CUSTOM_LABELS_FOLDER = {
    'name':            'Folder Name',
    'parent':          'Parent Folder',
    'project':         'Project',
    'company_profile': 'Company Profile',
    'freelancer':      'Freelancer',
    'created_at':      'Created At',
    'updated_at':      'Updated At',
}

CUSTOM_LABELS_INVOICE = {
    'freelancer':         'Freelancer',
    'project':            'Project',
    'company_profile':    'Company Profile',
    'reciepient_user':    'Recipient User',
    'reciepient_company': 'Recipient Company',
    'invoice_date':       'Invoice Date',
    'due_date':           'Due Date',
    'status':             'Status',
    'grand_total':        'Grand Total',
    'notes':              'Notes',
    'vat_rate':           'VAT Rate',
    'documents':          'Attached Documents',
    'created_at':         'Created At',
    'updated_at':         'Updated At',
}

CUSTOM_LABELS_CALENDAR_EVENT = {
    'company_profile': 'Company Profile',
    'freelancer': 'Freelancer',
    'name': 'Event Name',
    'description': 'Description',
    'start_date': 'Start Date & Time',
    'end_date': 'End Date & Time',
    'user': 'Created By',
    'outlook_event_id': 'Outlook Event ID',
    'assignees': 'Assignees',
    'documents': 'Attached Documents',
    'meeting_link': 'Meeting Link',
    'created_at': 'Created At',
    'updated_at': 'Updated At',
}

CUSTOM_LABELS_OPPORTUNITY = {
    'created_by':                   'Created By',
    'freelancer':                   'Freelancer',
    'company_profile':              'Company Profile',
    'title':                        'Title',
    'details':                      'Details',
    'status':                       'Status',
    'customer_user':                'Customer User',
    'customer_company':             'Customer Company',
    'country':                      'Country',
    'revenue':                      'Revenue',
    'probability':                  'Probability (%)',
    'contract_date':                'Contract Date',
    'planned_delivery_start_date':  'Planned Start Date',
    'planned_delivery_end_date':    'Planned End Date',
    'purchase_order':               'Purchase Order',
    'documents':                    'Attached Documents',
    'approver':                     'Approver',
    'sent_date':                    'Sent Date',
    'value':                        'Value',
    'sales_phase':                  'Sales Phase',
    'close_reason':                 'Close Reason',
    'created_at':                   'Created At',
    'updated_at':                   'Updated At',
}

CUSTOM_LABELS_STAFF_OBSERVATION = {
    'company_profile': 'Company Profile',
    'title':           'Title',
    'description':     'Description',
    'staff':           'Staff Member',
    'created_by':      'Created By',
    'created_at':      'Created At',
    'updated_at':      'Updated At',
}

CUSTOM_LABELS_LEAVE_REQUEST = {
    'employee':          'Employee',
    'manager':           'Manager',
    'type':              'Leave Type',
    'start_date':        'Start Date',
    'end_date':          'End Date',
    'is_working_from_home': 'WFH?',
    'status':            'Status',
    'reason':            'Reason',
}

CUSTOM_LABELS_CONTACT_USER = {
    'name': 'Name',
    'email': 'Email',
    'phone': 'Phone',
    'address': 'Address',
    'city': 'City',
    'state': 'State',
    'zip': 'Zip',
}

CUSTOM_LABELS_CONTACT_COMPANY = {
    'company_name': 'Company Name',
    'email': 'Email',
    'phone': 'Phone',
    'address': 'Address',
    'city': 'City',
    'state': 'State',
    'zip': 'Zip',
}
