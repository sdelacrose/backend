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
    is_wfh = request_type == "Work From Home"
    request_label = "Work From Home" if is_wfh else "Leave"
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
    is_wfh = request_type == "Work From Home"
    label = "Work From Home" if is_wfh else leave_type

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

    if status == "Approved":
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