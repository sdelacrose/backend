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
