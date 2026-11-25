from ecpmpapp.constants import StatusType, LeaveType
from django.utils import timezone
from ecpmpapp.utils import get_user_active_solution, get_frontend_url_for_solution
from project_management.models import ProjectTeamMembers

def plain_messages_for_send_approval_email(
    assignee,
    card_type,
    card_name,
    submitted_by,
    created_at,
    description,
    milestone_name=None,
    action_item_name=None,
    hours=None,
    category=None,
    rate=None,
    quantity=None,
    total=None,
    frontend_url=None,
):

    lines = [
        f"Dear {assignee},",
        "",
        f"A new {card_type} has been submitted and requires your review and approval.",
        "",
        "Details:",
        f"- Name: {card_name}",
        f"- Submitted By: {submitted_by}",
        f"- Date: {created_at}",
        f"- Description: {description}",
    ]

    if milestone_name:
        lines.append(f"- Milestone: {milestone_name}")
    if action_item_name:
        lines.append(f"- Action Item: {action_item_name}")

    if card_type == "Time Card" and hours is not None:
        lines.append(f"- Hours: {hours}")
    elif card_type == "Expense Card":
        lines.extend([
            f"- Category: {category}",
            f"- Rate: {rate}",
            f"- Quantity: {quantity}",
            f"- Total: {total}",
        ])

    lines.extend([
        "",
        "Please log in to the system to review and approve this card.",
        f"View the {card_type}: {frontend_url}" if frontend_url else "",
        "",
        "If you have already taken action, please disregard this email.",
        "",
        "Best regards,",
        "sharemundo team",
    ])

    return "\n".join(filter(None, lines))

def plain_messages_for_send_pending_approval_reminder_email(
    assignee,
    card_type,
    card_name,
    submitted_by,
    created_at,
    description,
    milestone_name=None,
    action_item_name=None,
    hours=None,
    category=None,
    rate=None,
    quantity=None,
    total=None,
    frontend_url=None,
):
    """
    Generate a plain-text reminder message for pending approval cards.

    :param assignee: Person who needs to approve
    :param card_type: "Time Card" or "Expense Card"
    :param card_name: Name/title of the card
    :param submitted_by: Who submitted the card
    :param created_at: Date of submission
    :param description: Description of the card
    :param milestone_name: Optional milestone name
    :param action_item_name: Optional action item
    :param hours: Used only for Time Card
    :param category: Used only for Expense Card
    :param rate: Used only for Expense Card
    :param quantity: Used only for Expense Card
    :param total: Used only for Expense Card
    :param frontend_url: Link to view the card
    """

    lines = [
        f"Dear {assignee},",
        "",
        f"This is a reminder that a {card_type} is pending your review and approval.",
        "",
        "Details:",
        f"- Name: {card_name}",
        f"- Submitted By: {submitted_by}",
        f"- Date: {created_at}",
        f"- Description: {description}",
    ]

    if milestone_name:
        lines.append(f"- Milestone: {milestone_name}")
    if action_item_name:
        lines.append(f"- Action Item: {action_item_name}")

    if card_type == "Time Card" and hours is not None:
        lines.append(f"- Hours: {hours}")
    elif card_type == "Expense Card":
        lines.extend([
            f"- Category: {category}",
            f"- Rate: {rate}",
            f"- Quantity: {quantity}",
            f"- Total: {total}",
        ])

    lines.extend([
        "",
        "Please log in to the system to review and approve this card.",
        f"View the {card_type}: {frontend_url}" if frontend_url else "",
        "",
        "Best regards,",
        "sharemundo team",
    ])

    return "\n".join(filter(None, lines))

def plain_messages_for_invoice_send_reminders(contact_name, invoice_no, due_date, amount, invoice_url=None):

    lines = [
        f"Dear {contact_name},",
        "",
        f"This is a friendly reminder that your invoice #{invoice_no} is due on {due_date}.",
        f"The total amount due is {amount}.",
        "",
        "Please ensure the payment is made to avoid any delays in service.",
    ]

    if invoice_url:
        lines.append(f"View your invoice: {invoice_url}")

    lines.extend([
        "",
        "Thank you for your attention to this matter.",
        "",
        "Best regards,",
        "sharemundo team"
    ])

    return "\n".join(lines)

def plain_message_for_invoice_due_reminder(contact_name, invoice_no, due_date, amount, invoice_url=None):
    lines = [
        f"Dear {contact_name},",
        "",
        f"This is a friendly reminder that invoice #{invoice_no} is due for payment in one week on {due_date}.",
        f"The total amount due is {amount}.",
        "",
        "To ensure uninterrupted service, please arrange for payment before the due date.",
    ]

    if invoice_url:
        lines.append(f"View your invoice: {invoice_url}")

    lines.extend([
        "",
        "If you have already made the payment, please disregard this reminder.",
        "",
        "Best regards,",
        "sharemundo team"
    ])

    return "\n".join(lines)

def plain_message_invoice_overdue(contact_name, invoice_no, due_date, amount, invoice_url=None):

    message = [
        f"Dear {contact_name},",
        "",
        f"We notice that invoice #{invoice_no}, which was due on {due_date}, is now overdue.",
        f"The outstanding amount is {amount}.",
        "",
        "Please ensure the payment is made to avoid any delays in service."
    ]

    if invoice_url:
        message.append(f"View your invoice: {invoice_url}")

    message += [
        "",
        "If the payment has already been made, could you please provide the payment details?",
        "",
        "Thank you for your attention to this matter.",
        "",
        "Best regards,",
        "sharemundo team"
    ]

    return "\n".join(message)

def plain_message_invoice_overdue_reminder(contact_name, invoice_no, due_date, days_overdue, amount, invoice_url=None):
    message = [
        f"Dear {contact_name},",
        "",
        f"This is a reminder that invoice #{invoice_no} remains unpaid. The invoice was due on {due_date} and is now {days_overdue} days overdue.",
        f"The outstanding amount is {amount}.",
        "",
        "Please ensure the payment is made to avoid any delays in service."
    ]

    if invoice_url:
        message.append(f"View your invoice: {invoice_url}")

    message += [
        "",
        "If the payment has already been made, could you please provide the payment details?",
        "",
        "Thank you for your attention to this matter.",
        "",
        "Best regards,",
        "sharemundo team"
    ]

    return "\n".join(message)

def plain_message_action_item_reminder(first_name, name, project_name, due_date, reminder_settings, frontend_url=None):

    message = [
        f"Dear {first_name},",
        "",
        f"This is a friendly reminder regarding project phase \"{name}\" for the project {project_name}. According to our records, the due date for this project phase is {due_date}.",
        "",
        "Project Phase details:",
        f"Project Phase Name: {name}",
        f"Project Name: {project_name}",
        f"Due Date: {due_date}",
        f"Reminder Settings: {reminder_settings} (e.g., 2 days before)",
        "",
        "Please make sure to review and address any outstanding issues related to this project phase to ensure timely completion.",
        "",
        "If you have any questions or need further assistance, please do not hesitate to contact us."
    ]

    if frontend_url:
        message.append(f"View the project phase: {frontend_url}")

    message += [
        "",
        "Thank you for your attention to this matter.",
        "",
        "Best regards,",
        "sharemundo team"
    ]

    return "\n".join(message)

def plain_message_action_item_reminder_with_milestone(
    assignee, name, milestone_name, project_name,
    target_date, reminder_settings, frontend_url=None
):
    message = [
        f"Dear {assignee},",
        "",
        f"This is a friendly reminder regarding action item \"{name}\" for the milestone \"{milestone_name}\" of the project \"{project_name}\".",
        f"According to our records, the due date for this action item is {target_date}.",
        "",
        "Action Item Details:",
        f" - Action Item Name: {name}",
        f" - Milestone Name: {milestone_name}",
        f" - Project Name: {project_name}",
        f" - Due Date: {target_date}",
        f" - Reminder Settings: {reminder_settings}",
        f" - Assignee: {assignee}",
        "",
        "Please make sure to review and address any outstanding tasks related to this action item to ensure timely completion.",
    ]

    if frontend_url:
        message.append(f"View action items: {frontend_url}")

    message += [
        "",
        "If you have any questions or need further assistance, please do not hesitate to contact us.",
        "",
        "Thank you for your attention to this matter.",
        "",
        "Best regards,",
        "sharemundo team"
    ]

    return "\n".join(message)

def plain_message_action_item_reminder_with_company(
    assignee, name, target_date, reminder_settings,
    frontend_url=None, company_name=None
):
    message = [
        f"Dear {assignee},",
        "",
        f"This is a friendly reminder regarding action item \"{name}\"" +
        (f" for the company \"{company_name}\"" if company_name else "") +
        f". According to our records, the due date for this action item is {target_date}.",
        "",
        "Action Item Details:"
    ]

    # Add table-style detail section
    details = [
        f" - Action Item Name: {name}",
        f" - Due Date: {target_date}",
        f" - Reminder Settings: {reminder_settings}",
        f" - Assignee: {assignee}"
    ]
    if company_name:
        details.insert(1, f" - Company Name: {company_name}")
    
    message.extend(details)
    message.append("")

    # Add action button URL if present
    if frontend_url:
        message.append(f"View action items: {frontend_url}")
        message.append("")

    message.extend([
        "Please make sure to review and address any outstanding tasks related to this action item to ensure timely completion.",
        "",
        "If you have any questions or need further assistance, please do not hesitate to contact us.",
        "",
        "Thank you for your attention to this matter.",
        "",
        "Best regards,",
        "sharemundo team"
    ])

    return "\n".join(message)

def plain_messages_for_send_request_notification_email(
    manager_name,
    employee_name,
    request_type,
    leave_type,
    start_date,
    end_date,
    reason,
    approval_link,
):
    """
    Generate a plain-text notification for a manager when an employee submits
    a Work From Home or Leave request.
    """
    is_wfh = request_type == LeaveType.WORK_FROM_HOME.value
    request_label = LeaveType.WORK_FROM_HOME.value if is_wfh else LeaveType.LEAVE.value
    leave_label = "" if is_wfh else leave_type

    lines = [
        f"Dear {manager_name},",
        "",
        f"{employee_name} has entered a {request_label} request.",
        "",
        "Request Details:",
        f"- Type: {request_label if is_wfh else leave_label}",
        f"- Start Date: {start_date}",
        f"- End Date: {end_date}",
        f"- Reason: {reason}",
        "",
        f"View and approve or reject this request here: {approval_link}",
        "",
        "Best regards,",
        "sharemundo team",
    ]

    return "\n".join(lines)

def plain_messages_for_send_request_status_email(
    employee_name,
    request_type,
    leave_type,
    start_date,
    end_date,
    reason,
    status,
    manager_name,
):
    """
    Generate a plain-text notification to an employee about their request status.

    :param employee_name: Name of the employee
    :param request_type: "Work From Home" or other (treated as leave)
    :param leave_type: Type of leave if request_type != "Work From Home"
    :param start_date: Request start date
    :param end_date: Request end date
    :param reason: Reason for the request
    :param status: "Approved" or "Rejected"
    :param manager_name: Name of the manager who took action
    """
    is_wfh = request_type == LeaveType.WORK_FROM_HOME.value
    label = LeaveType.WORK_FROM_HOME.value if is_wfh else leave_type

    lines = [
        f"Dear {employee_name},",
        "",
        f"Your {label} request has been {status} by {manager_name}.",
        "",
        "Request Details:",
        f"- Type: {label}",
        f"- Start Date: {start_date}",
        f"- End Date: {end_date}",
        f"- Reason: {reason}",
        ""
    ]

    if status.lower() == StatusType.APPROVED.value:
        if is_wfh:
            lines.append("Please ensure you remain available and reachable during working hours.")
        else:
            lines.append("Enjoy your time off!")
    else:
        lines.append("If you need further clarification, please reach out to your manager.")

    lines.extend([
        "",
        "Best regards,",
        "sharemundo team",
    ])

    return "\n".join(lines)

def plain_message_for_send_project_welcome_email(
    project,
    new_project_team_member,
    requester
):
    """
    Generate a plain‑text notification for a user when they are added
    as a project member on Sharemundo Links.
    """
    user_name = new_project_team_member.team_member.full_name
    project_name = project.project_name
    request_date = timezone.now().strftime("%d-%m-%Y")
    user_solution = get_frontend_url_for_solution(get_user_active_solution(new_project_team_member.team_member.id))
    access_level = new_project_team_member.role
    access_rights = new_project_team_member.project_access

    membership = ProjectTeamMembers.objects.filter(
        project=project,
        team_member=requester
    ).first()

    if membership:
        requester_role   = membership.role
    else:
        requester_role   = 'Unknown'
    lines = [
        f"Dear {user_name},",
        "",
        "You have been added as a project member on our Sharemundo Links platform.",
        "",
        "Project Details:",
        f" - Project Name    : {project_name}",
        f" - Requested By    : {requester}",
        f" - Requester Role  : {requester_role}",
        f" - Date of Request : {request_date}",
        f" - Access Level    : {access_level}",
        f" - Access Rights   : {access_rights}",
        "",
        f"You now have access to the project and its features through your assigned solution: {user_solution}.",
        "To view the project, simply select it from the dropdown menu at the bottom of the sidebar.",
        "",
        "We are happy to have you on board!",
        "",
        "Best regards,",
        "sharemundo team",
        "",
    ]

    return "\n".join(lines)
def plain_messages_for_send_feedback_notification_email(
    user_name,
    solution_name,
    feedback_type,
    feedback_link,
):
    lines = [
        f"Dear administrators,",
        "",
        f"Some feedback on {solution_name} has been added.",
        "",
        "Quick Details:",
        f"- User name: {user_name}",
        f"- Type of feedback: {feedback_type}",
        "",
        f"Please click {feedback_link} to see the complete feedback form and take necessary actions.",
        "",
        "Best regards,",
        "sharemundo team",
    ]

    return "\n".join(lines)


def plain_message_for_send_invitation_email_to_external_user(
    new_project_team_member,
    requester
):
    """
    Generate a plain‑text invitation for an external user when they are
    invited to join a project on Sharemundo Links.
    """
    # Gather data
    user_name = new_project_team_member.team_member.full_name
    invitation_sender_name = requester.full_name

    # Build message lines
    lines = [
        f"Dear {user_name},",
        "",
        f"You’ve been invited to join sharemundo links by {invitation_sender_name}.",
        "",
        "As you are not yet a member of our community, you'll first need to",
        "register on our platform to access its features and project details.",
        "",
        "You can choose to either:",
        "- Start a 14‑day free trial (which will automatically renew unless cancelled), or",
        "- Purchase a license for one of our available solutions.",
        "",
        "To learn more about our platform and available offers, visit:",
        "https://sharemundo.com/",
        "",
        "We look forward to welcoming you on board!",
        "",
        "Best regards,",
        "sharemundo team",
        "",
    ]

    return "\n".join(lines)

def plain_messages_for_expiring_po_reminder(contact_name, po):
    lines = [
        f"Dear {contact_name},",
        "",
        f"This is a friendly reminder that purchase order #{po.po_number} is expiring on {po.end_date}.",
        "",
        "Best regards,",
        "sharemundo team",
        "",
    ]
    return "\n".join(lines)


def plain_message_leave_balance_reminder(employee_name, deadline_date, balances):
    """
    AI-1685: plain-text leave-balance reminder ahead of a contract end date
    or configured deadline. `balances` is the same list of dicts
    services.leave_reminders.compute_balance_snapshot builds (available/
    reserved/carry_over_available/carry_over_expires_on per leave type) --
    never salary/hourly_rate, which are never read by that function.
    """
    lines = [
        f"Dear {employee_name},",
        "",
        f"This is a reminder to review your leave balance ahead of {deadline_date}.",
        "",
        "Current balance by leave type:",
    ]
    for b in balances:
        line = f"- {b['leave_type_name']}: {b['available']} available (reserved: {b['reserved']})"
        if b.get("carry_over_available"):
            expiry = b.get("carry_over_expires_on") or "no expiry recorded"
            line += f", including a carry-over of {b['carry_over_available']} expiring {expiry}"
        lines.append(line)
    lines.extend([
        "",
        "This reflects only currently available balance -- it is not a promise "
        "that every right shown above will remain usable until your departure, "
        "especially any carry-over expiring before then.",
        "",
        "Best regards,",
        "sharemundo team",
    ])
    return "\n".join(lines)




