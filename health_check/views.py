import os
import uuid
from pathlib import Path
import time
import json
import requests
from django.conf import settings
from rest_framework.viewsets import ViewSet, ReadOnlyModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response
from datetime import datetime
from models.models import (
    User,
    APITestResult,
    APITestRun,
)
from pathlib import Path
from .utils import override_base_url
from .auth0_client import get_headers_with_cached_token
from datetime import datetime, timezone
import os

from project_management.pagination import CustomPageNumberPagination
from health_check.serializers import APITestResultSerializer
from health_check.authentication import APIKeyAuthentication

import logging


def get_base_url():
    """
    Get the base URL for health check endpoints from environment variables.
    Falls back to localhost if not configured.
    """
    # Try to get from environment variables
    base_url = settings.BACKEND_URL
    if not base_url:
        # Fallback to settings
        base_url = 'http://localhost:8000'
    
    # Ensure the base URL doesn't end with a slash
    return base_url.rstrip('/')


def construct_full_url(path_url):
    """
    Construct a full URL from a path-only URL by prepending the base URL.
    
    Args:
        path_url (str): URL path (e.g., '/api/project_management/action_items/')
    
    Returns:
        str: Full URL with base URL prepended
    """
    base_url = get_base_url()
    
    # Ensure path starts with /
    if not path_url.startswith('/'):
        path_url = '/' + path_url
    
    return f"{base_url}{path_url}"


def generate_curl_command(method, url, headers, payload=None):
    """Generate a curl command for debugging failed requests."""
    # Create a simpler curl command that's easier to debug
    curl_cmd = f"curl -X {method}"

    # Add essential headers only
    if "Authorization" in headers:
        curl_cmd += f' -H "Authorization: {headers["Authorization"]}"'
    if "Content-Type" in headers:
        curl_cmd += f' -H "Content-Type: {headers["Content-Type"]}"'
    if "X-API" in headers:
        curl_cmd += f' -H "X-API: {headers["X-API"]}"'

    # Add payload for POST, PUT, PATCH
    if payload and method in ["POST", "PUT", "PATCH"]:
        import json

        # Create a minimal payload for debugging
        if isinstance(payload, dict):
            # Just show the keys and first few values for debugging
            debug_payload = {}
            for key, value in list(payload.items())[:3]:  # Show first 3 fields
                if isinstance(value, str) and len(value) > 50:
                    debug_payload[key] = value[:50] + "..."
                else:
                    debug_payload[key] = value
            payload_str = json.dumps(debug_payload)
        else:
            payload_str = (
                str(payload)[:100] + "..." if len(str(payload)) > 100 else str(payload)
            )

        curl_cmd += f" -d '{payload_str}'"

    # Add URL
    curl_cmd += f' "{url}"'

    return curl_cmd


# Get admin user for testing - you may need to adjust this based on your backend setup
def get_admin_user():
    try:
        # Try to get a user with staff profile first
        user_with_staff = User.objects.filter(staff__isnull=False).first()
        if user_with_staff:
            return user_with_staff
        # Fallback to the original user
        return User.objects.get(id=537)
    except User.DoesNotExist:
        # Create a default admin user if it doesn't exist
        return User.objects.create_user(
            email="admin@healthcheck.com",
            password="StrongPassword123!",
            username="healthcheck_admin",
            first_name="Health",
            last_name="Check",
            user_type="company_user",
        )


admin_user = None  # Will be initialized when needed


def create_unique_user_and_update_payload(payload_path: Path, headers):
    """
    Creates a unique user and updates the 'admin_user' field in the given payload.json file.
    """
    unique_suffix = uuid.uuid4().hex[:6]
    email = f"user_{unique_suffix}@example.com"
    username = f"user_{unique_suffix}"
    sharemundo_links_id = f"linksuid_{unique_suffix}"
    iso_time = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    user = User.objects.create_user(
        email=email,
        password="StrongPassword123!",
        username=username,
        first_name="New",
        last_name="User",
        user_type="company_user",
        sharemundo_links_id=sharemundo_links_id,
    )

    user_id = user.id

    if not payload_path.exists():
        raise FileNotFoundError(f"{payload_path} not found")

    with open(payload_path, "r") as f:
        payload = json.load(f)

    # Update payload with admin user ID
    if "company_profile_payload" in payload:
        admin_user = get_admin_user()
        payload["company_profile_payload"]["admin_user"] = admin_user.id

    # Write back the updated payload
    with open(payload_path, "w") as f:
        json.dump(payload, f, indent=2)

    return user


class EndpointTestingViewSet(ViewSet):
    authentication_classes = [APIKeyAuthentication]
    permission_classes = []

    APP_DIR = Path(__file__).resolve().parent
    BASE_DIR = APP_DIR / "test_data"
    PAYLOAD_FILE = BASE_DIR / "payload.json"

    ENDPOINT_FILES = [
        "auth.json",
        "company_profiles.json",
        "company_config.json",
        "contact_company.json",
        "contact_user.json",
        "notice_board.json",
        "human_resource_staff.json",
        "human_resource_staff_observation.json",
        "human_resource_leave_requests.json",
        "project.json",
        "project_phase.json",
        "milestone.json",  # Move milestone before action_items
        "action_items.json",  # Now action_items comes after milestone
        "opportunity.json",
        "expense_card.json",
        "site_reporting.json",
        "time_card.json",
        "events.json",
        "feedback.json",
        "chat.json",
        "invoice.json",
        "document.json",
        # "project_management.json"
    ]

    def load_payloads(self):
        # Get fresh headers for payload creation
        headers = get_headers_with_cached_token()
        create_unique_user_and_update_payload(self.PAYLOAD_FILE, headers)
        with open(self.PAYLOAD_FILE, "r") as f:
            return json.load(f)

    def load_endpoints(self, file_path):
        with open(file_path, "r") as f:
            return json.load(f)

    def check_endpoints_with_global_ids(self, endpoints, payloads, global_created_ids):
        """
        Check endpoints with global ID tracking that persists across files.
        This ensures that IDs created in previous files are available in subsequent files.
        """
        # Get fresh headers with cached Auth0 token
        headers = get_headers_with_cached_token()

        results = []
        # Use the global created IDs passed from the parent method
        created_ids = (
            global_created_ids.copy()
        )  # Work with a copy to avoid modifying the original

        # Fallback IDs from database (only used if no POST was successful)
        fallback_ids = {
            "notice_board": None,
            "opportunity": None,
            "company_config": None,
            "staff": None,
            "staff_observation": None,
            "leave_request": None,
            "project_phase": None,
            "milestone": None,
            "contact_company": None,
            "contact_user": None,
            "action_items": None,
            "expense_card": None,
            "site_reporting": None,
            "time_card": None,
            "events": None,
            "feedback": None,
            "invoice": None,
            "user": None,
            "company": None,
            "project": None,
        }
        # Fallback IDs from database (only used if no POST was successful)
        fallback_ids = {
            "notice_board": None,
            "opportunity": None,
            "company_config": None,
            "staff": None,
            "staff_observation": None,
            "leave_request": None,
            "project_phase": None,
            "milestone": None,
            "contact_company": None,
            "contact_user": None,
            "action_items": None,
            "expense_card": None,
            "site_reporting": None,
            "time_card": None,
            "events": None,
            "feedback": None,
            "invoice": None,
            "user": None,
            "company": None,
            "project": None,
        }

        for ep in endpoints:
            path_url = ep["url"]
            # Construct full URL from path-only URL
            url = construct_full_url(path_url)
 
            method = ep["method"]
            payload_name = ep.get("payload_name")
            payload = (
                payloads.get(payload_name, {})
                if payload_name
                else ep.get("payload", {})
            )
            expected = ep.get("expected_status", 200)

            # Handle dynamic ID replacement for PATCH, PUT, DELETE, GET with {id} in URL
            if method in ["PUT", "PATCH", "DELETE", "GET"] and "{id}" in url:
                if "notice-board" in url:
                    # Use created ID first, then fallback to database lookup
                    if created_ids["notice_board"]:
                        replacement_id = created_ids["notice_board"]
                    else:
                        if fallback_ids["notice_board"] is None:
                            # Try to get notice board ID from database
                            try:
                                from companyprofileapis.models import NoticeBoard

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    notice = NoticeBoard.objects.filter(
                                        company_profile=admin_user.company
                                    ).first()
                                    if notice:
                                        fallback_ids["notice_board"] = str(notice.id)
                                    else:
                                        # Try to get any notice board
                                        notice = NoticeBoard.objects.first()
                                        if notice:
                                            fallback_ids["notice_board"] = str(
                                                notice.id
                                            )
                                        else:
                                            fallback_ids["notice_board"] = (
                                                "1"  # fallback
                                            )
                                else:
                                    # Try to get any notice board
                                    notice = NoticeBoard.objects.first()
                                    if notice:
                                        fallback_ids["notice_board"] = str(notice.id)
                                    else:
                                        fallback_ids["notice_board"] = "1"  # fallback
                            except:
                                fallback_ids["notice_board"] = "1"  # fallback
                        replacement_id = fallback_ids["notice_board"]
                elif "opportunity" in url:
                    # Use created ID first, then fallback to database lookup
                    if created_ids["opportunity"]:
                        replacement_id = created_ids["opportunity"]
                    else:
                        if fallback_ids["opportunity"] is None:
                            # Try to get opportunity ID from database
                            try:
                                from companyprofileapis.models import Opportunity

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    opportunity = Opportunity.objects.filter(
                                        company_profile=admin_user.company
                                    ).first()
                                    if opportunity:
                                        fallback_ids["opportunity"] = str(
                                            opportunity.id
                                        )
                                    else:
                                        # Try to get any opportunity
                                        opportunity = Opportunity.objects.first()
                                        if opportunity:
                                            fallback_ids["opportunity"] = str(
                                                opportunity.id
                                            )
                                        else:
                                            fallback_ids["opportunity"] = (
                                                "1"  # fallback
                                            )
                                else:
                                    # Try to get any opportunity
                                    opportunity = Opportunity.objects.first()
                                    if opportunity:
                                        fallback_ids["opportunity"] = str(
                                            opportunity.id
                                        )
                                    else:
                                        fallback_ids["opportunity"] = "1"  # fallback
                            except:
                                fallback_ids["opportunity"] = "1"  # fallback
                        replacement_id = fallback_ids["opportunity"]
                elif "staff-observation" in url or "staff_observation" in url:
                    # Use created ID first, then fallback to database lookup
                    if created_ids["staff_observation"]:
                        replacement_id = created_ids["staff_observation"]
                    else:
                        if fallback_ids["staff_observation"] is None:
                            # Try to get staff observation ID from database
                            try:
                                from human_resource_management.models import (
                                    StaffObservation,
                                )

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    observation = StaffObservation.objects.filter(
                                        company_profile=admin_user.company
                                    ).first()
                                    if observation:
                                        fallback_ids["staff_observation"] = str(
                                            observation.id
                                        )
                                    else:
                                        # Try to get any staff observation
                                        observation = StaffObservation.objects.first()
                                        if observation:
                                            fallback_ids["staff_observation"] = str(
                                                observation.id
                                            )
                                        else:
                                            fallback_ids["staff_observation"] = (
                                                "1"  # fallback
                                            )
                                else:
                                    # Try to get any staff observation
                                    observation = StaffObservation.objects.first()
                                    if observation:
                                        fallback_ids["staff_observation"] = str(
                                            observation.id
                                        )
                                    else:
                                        fallback_ids["staff_observation"] = (
                                            "1"  # fallback
                                        )
                            except Exception as e:
                                fallback_ids["staff_observation"] = "1"  # fallback
                        replacement_id = fallback_ids["staff_observation"]
                elif "staff" in url and "human_resource_management" in url:
                    # Use created ID first, then fallback to database lookup
                    if created_ids["staff"]:
                        replacement_id = created_ids["staff"]
                    else:
                        if fallback_ids["staff"] is None:
                            # Try to get staff ID from database
                            try:
                                from models.models import Staff

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    staff = Staff.objects.filter(
                                        user__company=admin_user.company
                                    ).first()
                                    if staff:
                                        fallback_ids["staff"] = str(staff.id)
                                    else:
                                        # Try to get any staff
                                        staff = Staff.objects.first()
                                        if staff:
                                            fallback_ids["staff"] = str(staff.id)
                                        else:
                                            fallback_ids["staff"] = "1"  # fallback
                                else:
                                    # Try to get any staff
                                    staff = Staff.objects.first()
                                    if staff:
                                        fallback_ids["staff"] = str(staff.id)
                                    else:
                                        fallback_ids["staff"] = "1"  # fallback
                            except Exception as e:
                                fallback_ids["staff"] = "1"  # fallback
                        replacement_id = fallback_ids["staff"]
                elif "leave-requests" in url:
                    # Use created ID first, then fallback to database lookup
                    if created_ids["leave_request"]:
                        replacement_id = created_ids["leave_request"]
                    else:
                        if fallback_ids["leave_request"] is None:
                            # Try to get leave request ID from database
                            try:
                                from human_resource_management.models import (
                                    LeaveRequest,
                                )

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    leave_request = LeaveRequest.objects.filter(
                                        employee__user__company=admin_user.company
                                    ).first()
                                    if leave_request:
                                        fallback_ids["leave_request"] = str(
                                            leave_request.id
                                        )
                                    else:
                                        # Try to get any leave request
                                        leave_request = LeaveRequest.objects.first()
                                        if leave_request:
                                            fallback_ids["leave_request"] = str(
                                                leave_request.id
                                            )
                                        else:
                                            fallback_ids["leave_request"] = (
                                                "1"  # fallback
                                            )
                                else:
                                    # Try to get any leave request
                                    leave_request = LeaveRequest.objects.first()
                                    if leave_request:
                                        fallback_ids["leave_request"] = str(
                                            leave_request.id
                                        )
                                    else:
                                        fallback_ids["leave_request"] = "1"  # fallback
                            except Exception as e:
                                fallback_ids["leave_request"] = "1"  # fallback
                        replacement_id = fallback_ids["leave_request"]
                elif "company-config" in url:
                    # Use created ID first, then fallback to database lookup
                    if created_ids["company_config"]:
                        replacement_id = created_ids["company_config"]
                    else:
                        if fallback_ids["company_config"] is None:
                            # Try to get company config ID from database
                            try:
                                from models.models import CompanyConfig

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    config = CompanyConfig.objects.filter(
                                        company_profile=admin_user.company
                                    ).first()
                                    if config:
                                        fallback_ids["company_config"] = str(config.id)
                                    else:
                                        # Try to get any company config
                                        config = CompanyConfig.objects.first()
                                        if config:
                                            fallback_ids["company_config"] = str(
                                                config.id
                                            )
                                        else:
                                            fallback_ids["company_config"] = (
                                                "1"  # fallback
                                            )
                                else:
                                    # Try to get any company config
                                    config = CompanyConfig.objects.first()
                                    if config:
                                        fallback_ids["company_config"] = str(config.id)
                                    else:
                                        fallback_ids["company_config"] = "1"  # fallback
                            except:
                                fallback_ids["company_config"] = "1"  # fallback
                        replacement_id = fallback_ids["company_config"]
                elif "project-phase" in url:
                    # Use created ID first, then fallback to database lookup
                    if created_ids["project_phase"]:
                        replacement_id = created_ids["project_phase"]
                    else:
                        if fallback_ids["project_phase"] is None:
                            # Try to get project phase ID from database
                            try:
                                from project_management.models import ProjectPhase

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    project_phase = ProjectPhase.objects.filter(
                                        project__user__company=admin_user.company
                                    ).first()
                                    if project_phase:
                                        fallback_ids["project_phase"] = str(
                                            project_phase.id
                                        )
                                    else:
                                        # Try to get any project phase
                                        project_phase = ProjectPhase.objects.first()
                                        if project_phase:
                                            fallback_ids["project_phase"] = str(
                                                project_phase.id
                                            )
                                        else:
                                            fallback_ids["project_phase"] = (
                                                "1"  # fallback
                                            )
                                else:
                                    # Try to get any project phase
                                    project_phase = ProjectPhase.objects.first()
                                    if project_phase:
                                        fallback_ids["project_phase"] = str(
                                            project_phase.id
                                        )
                                    else:
                                        fallback_ids["project_phase"] = "1"  # fallback
                            except Exception as e:
                                fallback_ids["project_phase"] = "1"  # fallback
                        replacement_id = fallback_ids["project_phase"]
                elif "milestone" in url:
                    # Use created ID first, then fallback to database lookup
                    if created_ids["milestone"]:
                        replacement_id = created_ids["milestone"]
                    else:
                        if fallback_ids["milestone"] is None:
                            # Try to get milestone ID from database
                            try:
                                from project_management.models import Milestone

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    milestone = Milestone.objects.filter(
                                        project__user__company=admin_user.company
                                    ).first()
                                    if milestone:
                                        fallback_ids["milestone"] = str(milestone.id)
                                    else:
                                        # Try to get any milestone
                                        milestone = Milestone.objects.first()
                                        if milestone:
                                            fallback_ids["milestone"] = str(
                                                milestone.id
                                            )
                                        else:
                                            fallback_ids["milestone"] = (
                                                "1325"  # fallback to existing milestone
                                            )
                                else:
                                    # Try to get any milestone
                                    milestone = Milestone.objects.first()
                                    if milestone:
                                        fallback_ids["milestone"] = str(milestone.id)
                                    else:
                                        fallback_ids["milestone"] = (
                                            "1325"  # fallback to existing milestone
                                        )
                            except Exception as e:
                                fallback_ids["milestone"] = (
                                    "1325"  # fallback to existing milestone
                                )
                        replacement_id = fallback_ids["milestone"]
                elif "contact-company" in url:
                    if created_ids["contact_company"]:
                        replacement_id = created_ids["contact_company"]
                    else:
                        if fallback_ids["contact_company"] is None:
                            try:
                                from project_management.models import ContactCompany

                                contact_company = ContactCompany.objects.first()
                                if contact_company:
                                    fallback_ids["contact_company"] = str(
                                        contact_company.id
                                    )
                                else:
                                    fallback_ids["contact_company"] = "1"
                            except Exception as e:
                                fallback_ids["contact_company"] = "1"
                        replacement_id = fallback_ids["contact_company"]
                elif "contact-user" in url:
                    if created_ids["contact_user"]:
                        replacement_id = created_ids["contact_user"]
                    else:
                        if fallback_ids["contact_user"] is None:
                            try:
                                from models.models import User

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    contact_user = User.objects.filter(
                                        company=admin_user.company
                                    ).first()
                                    if contact_user:
                                        fallback_ids["contact_user"] = str(
                                            contact_user.id
                                        )
                                    else:
                                        fallback_ids["contact_user"] = "1"
                                else:
                                    fallback_ids["contact_user"] = "1"
                            except Exception as e:
                                fallback_ids["contact_user"] = "1"
                        replacement_id = fallback_ids["contact_user"]
                elif "action_items" in url:
                    if created_ids["action_items"]:
                        replacement_id = created_ids["action_items"]
                    else:
                        if fallback_ids["action_items"] is None:
                            try:
                                from project_management.models import ActionItem

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    action_item = ActionItem.objects.filter(
                                        company_profile=admin_user.company
                                    ).first()
                                    if action_item:
                                        fallback_ids["action_items"] = str(
                                            action_item.id
                                        )
                                    else:
                                        fallback_ids["action_items"] = "1"
                                else:
                                    fallback_ids["action_items"] = "1"
                            except Exception as e:
                                fallback_ids["action_items"] = "1"
                        replacement_id = fallback_ids["action_items"]
                elif "expense-card" in url:
                    if created_ids["expense_card"]:
                        replacement_id = created_ids["expense_card"]
                    else:
                        if fallback_ids["expense_card"] is None:
                            try:
                                from project_management.models import ExpenseCard

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    expense_card = ExpenseCard.objects.filter(
                                        project__user__company=admin_user.company
                                    ).first()
                                    if expense_card:
                                        fallback_ids["expense_card"] = str(
                                            expense_card.id
                                        )
                                    else:
                                        # Try to get any expense card
                                        expense_card = ExpenseCard.objects.first()
                                        if expense_card:
                                            fallback_ids["expense_card"] = str(
                                                expense_card.id
                                            )
                                        else:
                                            fallback_ids["expense_card"] = "1"
                                else:
                                    # Try to get any expense card
                                    expense_card = ExpenseCard.objects.first()
                                    if expense_card:
                                        fallback_ids["expense_card"] = str(
                                            expense_card.id
                                        )
                                    else:
                                        fallback_ids["expense_card"] = "1"
                            except Exception as e:
                                fallback_ids["expense_card"] = "1"
                        replacement_id = fallback_ids["expense_card"]
                elif "site-reporting" in url:
                    if created_ids["site_reporting"]:
                        replacement_id = created_ids["site_reporting"]
                    else:
                        if fallback_ids["site_reporting"] is None:
                            try:
                                from project_management.models import SiteReport

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    site_report = SiteReport.objects.filter(
                                        project__user__company=admin_user.company
                                    ).first()
                                    if site_report:
                                        fallback_ids["site_reporting"] = str(
                                            site_report.id
                                        )
                                    else:
                                        # Try to get any site report
                                        site_report = SiteReport.objects.first()
                                        if site_report:
                                            fallback_ids["site_reporting"] = str(
                                                site_report.id
                                            )
                                        else:
                                            fallback_ids["site_reporting"] = "1"
                                else:
                                    # Try to get any site report
                                    site_report = SiteReport.objects.first()
                                    if site_report:
                                        fallback_ids["site_reporting"] = str(
                                            site_report.id
                                        )
                                    else:
                                        fallback_ids["site_reporting"] = "1"
                            except Exception as e:
                                fallback_ids["site_reporting"] = "1"
                        replacement_id = fallback_ids["site_reporting"]
                elif "time-card" in url:
                    if created_ids["time_card"]:
                        replacement_id = created_ids["time_card"]
                    else:
                        if fallback_ids["time_card"] is None:
                            try:
                                from project_management.models import TimeCard

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    time_card = TimeCard.objects.filter(
                                        project__user__company=admin_user.company
                                    ).first()
                                    if time_card:
                                        fallback_ids["time_card"] = str(time_card.id)
                                    else:
                                        # Try to get any time card
                                        time_card = TimeCard.objects.first()
                                        if time_card:
                                            fallback_ids["time_card"] = str(
                                                time_card.id
                                            )
                                        else:
                                            fallback_ids["time_card"] = "1"
                                else:
                                    # Try to get any time card
                                    time_card = TimeCard.objects.first()
                                    if time_card:
                                        fallback_ids["time_card"] = str(time_card.id)
                                    else:
                                        fallback_ids["time_card"] = "1"
                            except Exception as e:
                                fallback_ids["time_card"] = "1"
                        replacement_id = fallback_ids["time_card"]
                elif "events" in url:
                    if created_ids["events"]:
                        replacement_id = created_ids["events"]
                    else:
                        if fallback_ids["events"] is None:
                            try:
                                from userapis.models import CalendarEvent

                                admin_user = get_admin_user()
                                # Try to get an event for the admin user
                                event = CalendarEvent.objects.filter(
                                    user=admin_user,
                                    assignees__in=[admin_user.id]
                                ).first()
                                if event:
                                    fallback_ids["events"] = str(event.id)
                                else:
                                    # Try to get any event
                                    event = CalendarEvent.objects.first()
                                    if event:
                                        fallback_ids["events"] = str(event.id)
                                    else:
                                        fallback_ids["events"] = "1"
                            except Exception as e:
                                fallback_ids["events"] = "1"
                        replacement_id = fallback_ids["events"]
                elif "feedback" in url:
                    if created_ids["feedback"]:
                        replacement_id = created_ids["feedback"]
                    else:
                        if fallback_ids["feedback"] is None:
                            try:
                                from models.models import Feedback

                                admin_user = get_admin_user()
                                # Try to get feedback for the admin user
                                feedback = Feedback.objects.filter(
                                    user=admin_user
                                ).first()
                                if feedback:
                                    fallback_ids["feedback"] = str(feedback.id)
                                else:
                                    # Try to get any feedback
                                    feedback = Feedback.objects.first()
                                    if feedback:
                                        fallback_ids["feedback"] = str(feedback.id)
                                    else:
                                        fallback_ids["feedback"] = "1"
                            except Exception as e:
                                fallback_ids["feedback"] = "1"
                        replacement_id = fallback_ids["feedback"]
                elif "billing-invoice" in url:
                    if created_ids["invoice"]:
                        replacement_id = created_ids["invoice"]
                    else:
                        if fallback_ids["invoice"] is None:
                            try:
                                from project_management.models import Invoice

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    invoice = Invoice.objects.filter(
                                        company_profile=admin_user.company
                                    ).first()
                                    if invoice:
                                        fallback_ids["invoice"] = str(invoice.id)
                                    else:
                                        # Try to get any invoice
                                        invoice = Invoice.objects.first()
                                        if invoice:
                                            fallback_ids["invoice"] = str(invoice.id)
                                        else:
                                            fallback_ids["invoice"] = "1"
                                else:
                                    # Try to get any invoice
                                    invoice = Invoice.objects.first()
                                    if invoice:
                                        fallback_ids["invoice"] = str(invoice.id)
                                    else:
                                        fallback_ids["invoice"] = "1"
                            except Exception as e:
                                fallback_ids["invoice"] = "1"
                        replacement_id = fallback_ids["invoice"]
                elif "projects" in url:
                    if created_ids["project"]:
                        replacement_id = created_ids["project"]
                    else:
                        if fallback_ids["project"] is None:
                            try:
                                from project_management.models import Project

                                admin_user = get_admin_user()
                                if admin_user.company:
                                    project = Project.objects.filter(
                                        user__company=admin_user.company
                                    ).first()
                                    if project:
                                        fallback_ids["project"] = str(project.id)
                                    else:
                                        # Try to get any project
                                        project = Project.objects.first()
                                        if project:
                                            fallback_ids["project"] = str(project.id)
                                        else:
                                            fallback_ids["project"] = "1"
                                else:
                                    # Try to get any project
                                    project = Project.objects.first()
                                    if project:
                                        fallback_ids["project"] = str(project.id)
                                    else:
                                        fallback_ids["project"] = "1"
                            except Exception as e:
                                fallback_ids["project"] = "1"
                        replacement_id = fallback_ids["project"]
                else:
                    admin_user = get_admin_user()
                    replacement_id = str(
                        admin_user.id
                    )  # Use admin user ID for other endpoints
                url = url.replace(
                    "{id}", str(replacement_id)
                )  # Ensure replacement_id is a string

            try:
                if method == "POST":
                    if "action_items" in url:
                        if created_ids["milestone"]:
                            payload["milestone"] = created_ids["milestone"]
                    if "opportunity" in url:
                        if created_ids["contact_user"]:
                            payload["customer_user"] = created_ids["contact_user"]
                    if "invoice" in url:
                        if created_ids["contact_user"]:
                            payload["reciepient_user"] = created_ids["contact_user"]
                    if "human_resource_management/staff-observation" in url:
                        if created_ids["staff"]:
                            payload["staff"] = created_ids["staff"]
                    if("project_management/projects" in url):
                        print(f"DEBUG: PROJECTS Created ID for {url}: {created_ids}")
                        if created_ids["company"]:
                            payload["company_profile"] = created_ids["company"]
                        if created_ids["user"]:
                            payload["user"] = created_ids["user"]
                    if("project_management/expense-card" in url):
                        if created_ids["project"]:
                            payload["project"] = created_ids["project"]
                    if("project_management/project-phase" in url):
                        if created_ids["project"]:
                            payload["project"] = created_ids["project"]
                    if("project_management/milestone" in url):
                        if created_ids["project"]:
                            payload["project"] = created_ids["project"]
                    if("project_management/time-card" in url):
                        if created_ids["project"]:
                            payload["project"] = created_ids["project"]
                    if("billing-invoice" in url):
                        if created_ids["project"]:
                            payload["project"] = created_ids["project"]

                    # Ensure all payload values are properly typed
                    for key, value in payload.items():
                        if isinstance(value, int):
                            payload[key] = str(value)

                    r = requests.post(url, json=payload, headers=headers, timeout=90)
                    if r.status_code in [200, 201]:
                        try:
                            response_data = r.json()
                            created_id = response_data.get("id")

                            print(f"DEBUG: Created ID for {url}: {created_id}")

                            # Store the created ID based on the endpoint type
                            if "notice-board" in url:
                                created_ids["notice_board"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "opportunity" in url:
                                created_ids["opportunity"] = (
                                    str(created_id) if created_id else None
                                )
                            elif (
                                "staff-observation" in url or "staff_observation" in url
                            ):
                                created_ids["staff_observation"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "leave-requests" in url:
                                created_ids["leave_request"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "project-phase" in url:
                                created_ids["project_phase"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "milestone" in url:
                                print(
                                    f"DEBUG: MILESTONE Created ID for {url}: {created_id}"
                                )
                                created_ids["milestone"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "contact-company" in url:
                                created_ids["contact_company"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "contact-user" in url:
                                created_ids["contact_user"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "action_items" in url:
                                print(
                                    f"DEBUG: ACTION ITEMSCreated ID for {url}: {created_id}"
                                )
                                created_ids["action_items"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "expense-card" in url:
                                print(
                                    f"DEBUG: EXPENSE CARD Created ID for {url}: {created_id}"
                                )
                                created_ids["expense_card"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "site-reporting" in url:
                                print(
                                    f"DEBUG: SITE REPORTING Created ID for {url}: {created_id}"
                                )
                                created_ids["site_reporting"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "time-card" in url:
                                print(
                                    f"DEBUG: TIME CARD Created ID for {url}: {created_id}"
                                )
                                created_ids["time_card"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "events" in url:
                                created_ids["events"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "feedback" in url:
                                created_ids["feedback"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "billing-invoice" in url:
                                created_ids["invoice"] = (
                                    str(created_id) if created_id else None
                                )
                            elif "project_management/projects" in url:
                                created_ids["project"] = (
                                    str(created_id) if created_id else None
                                )

                            print(f"DEBUG: Created ID for {url}: {created_id}")
                        except Exception as e:
                            print(f"DEBUG: Error parsing POST response: {e}")
                            pass

                elif method == "DELETE":
                    r = requests.delete(url, headers=headers, timeout=15)

                elif method == "GET":
                    r = requests.get(url, headers=headers, timeout=15)
                    if "human_resource_management/staff" in url:
                        try:
                            response_data = r.json()
                            if (
                                response_data.get("results")
                                and len(response_data["results"]) > 0
                            ):
                                staff_id = response_data["results"][0].get("id")
                                if staff_id is not None:
                                    created_ids["staff"] = str(
                                        staff_id
                                    )  # Convert to string
                                    print(
                                        f"DEBUG: Staff ID extracted: {created_ids['staff']}"
                                    )
                        except Exception as e:
                            print(f"DEBUG: Error extracting staff ID: {e}")
                    if("user/user_info" in url):
                        try:
                            response_data = r.json()
                            if response_data is not None:
                                created_ids['user'] = int(response_data['user']['id'])
                                created_ids['company'] = str(response_data['company']['id'])
                                print(f"DEBUG: User ID extracted: {created_ids['user']}")
                        except Exception as e:
                            print(f"DEBUG: Error extracting user ID: {e}")

                elif method == "PUT":
                    r = requests.put(url, json=payload, headers=headers, timeout=15)

                elif method == "PATCH":
                    r = requests.patch(url, json=payload, headers=headers, timeout=90)

                # Always generate curl command for debugging
                curl_command = generate_curl_command(
                    method,
                    url,
                    headers,
                    payload if method in ["POST", "PUT", "PATCH"] else None,
                )

                if r.status_code == expected:
                    test_result = f"test passed ({r.status_code})"
                    status = "up"
                else:
                    test_result = (
                        f"test failed (expected {expected}, got {r.status_code})"
                    )
                    status = "down"

            except Exception as e:
                status = f"down ({str(e)})"
                test_result = "exception occurred"
                curl_command = generate_curl_command(
                    method,
                    url,
                    headers,
                    payload if method in ["POST", "PUT", "PATCH"] else None,
                )

            result_item = {
                "url": url,
                "method": method,
                "status": status,
                "test_result": test_result,
                # "payload": payload,
            }

            results.append(result_item)

        # Update the global created IDs with any new IDs created in this file
        for key in global_created_ids:
            if created_ids[key] is not None:
                global_created_ids[key] = created_ids[key]
                print(f"DEBUG: Updated global ID for {key}: {created_ids[key]}")

        return results

    def check_endpoints(self, endpoints, payloads):
        """
        Legacy method for backward compatibility.
        Creates a new global_created_ids dict for each call.
        """
        # Create a fresh global_created_ids dict for this call
        global_created_ids = {
            "notice_board": None,
            "opportunity": None,
            "company_config": None,
            "staff": None,
            "staff_observation": None,
            "leave_request": None,
            "project_phase": None,
            "milestone": None,
            "contact_company": None,
            "contact_user": None,
            "action_items": None,
            "expense_card": None,
            "site_reporting": None,
            "time_card": None,
            "events": None,
            "feedback": None,
            "invoice": None,
            "user": None,
            "company": None,
            "project": None,
        }

        return self.check_endpoints_with_global_ids(
            endpoints, payloads, global_created_ids
        )

    @action(detail=False, methods=["get"])
    def test(self, request):
        api_test_run = APITestRun.objects.create(status="started")
        all_results = []

        # Track created IDs across all files - this will persist between JSON files
        global_created_ids = {
            "notice_board": None,
            "opportunity": None,
            "company_config": None,
            "staff": None,
            "staff_observation": None,
            "leave_request": None,
            "project_phase": None,
            "milestone": None,
            "contact_company": None,
            "contact_user": None,
            "action_items": None,
            "expense_card": None,
            "site_reporting": None,
            "time_card": None,
            "events": None,
            "feedback": None,
            "invoice": None,
            "user": None,
            "company": None,
            "project": None,
        }

        try:
            payloads = self.load_payloads()

            for file_name in self.ENDPOINT_FILES:
                print(f"DEBUG: Starting to process file: {file_name}")
                file_path = os.path.join(self.BASE_DIR, file_name)
                if not os.path.exists(file_path):
                    print(f"DEBUG: File {file_name} not found, skipping")
                    continue

                endpoints = self.load_endpoints(file_path)

                # Process this file's endpoints and get results with global ID tracking
                results = self.check_endpoints_with_global_ids(
                    endpoints, payloads, global_created_ids
                )

                print(f"DEBUG: Completed processing file: {file_name}")
                print(
                    f"DEBUG: Global created IDs after {file_name}: {global_created_ids}"
                )

                # Save each result set to DB
                APITestResult.objects.create(
                    file=file_name.removesuffix(".json"),
                    checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    results=results,
                    api_test_run=api_test_run,
                )

                all_results.append(
                    {
                        "file": file_name,
                        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "results": results,
                    }
                )

                # Wait between files to ensure proper sequencing
                print(f"DEBUG: Waiting 2 seconds before next file...")
                time.sleep(2)

            api_test_run.status = "finished"
            api_test_run.save()
            return Response(all_results)
        except Exception as ex:
            api_test_run.status = "failed"
            api_test_run.save()
            return Response(f"Error occurred: {str(ex)}")


class APITestRunViewSet(ReadOnlyModelViewSet):
    authentication_classes = [APIKeyAuthentication]
    permission_classes = []
    
    serializer_class = APITestResultSerializer
    pagination_class = CustomPageNumberPagination

    def get_queryset(self):
        try:
            last_test_run = APITestRun.objects.latest("started_at")
            return APITestResult.objects.filter(api_test_run=last_test_run)
        except APITestRun.DoesNotExist:
            return APITestResult.objects.none()

    @action(detail=False, methods=["get"])
    def history(self, request):
        """
        Get historical health check data with filtering options
        """
        # Get query parameters
        days = request.query_params.get("days", 7)  # Default to last 7 days
        status = request.query_params.get("status", None)  # Filter by status
        file_name = request.query_params.get("file", None)  # Filter by file

        try:
            days = int(days)
        except ValueError:
            days = 7

        # Calculate date range
        from datetime import timedelta

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        # Build queryset
        queryset = APITestRun.objects.filter(
            started_at__gte=start_date, started_at__lte=end_date
        )

        if status:
            queryset = queryset.filter(status=status)

        # Get the test runs
        test_runs = queryset.order_by("-started_at")

        # Get results for each test run
        results = []
        for test_run in test_runs:
            test_results = APITestResult.objects.filter(api_test_run=test_run)

            if file_name:
                test_results = test_results.filter(file__icontains=file_name)

            run_data = {
                "test_run_id": test_run.id,
                "started_at": test_run.started_at,
                "finished_at": test_run.finished_at,
                "status": test_run.status,
                "total_files_tested": test_results.count(),
                "results": [],
            }

            for result in test_results:
                # Calculate success rate for this file
                total_endpoints = len(result.results)
                successful_endpoints = sum(
                    1 for r in result.results if r.get("status") == "up"
                )
                failed_endpoints = total_endpoints - successful_endpoints

                file_result = {
                    "file": result.file,
                    "checked_at": result.checked_at,
                    "total_endpoints": total_endpoints,
                    "successful_endpoints": successful_endpoints,
                    "failed_endpoints": failed_endpoints,
                    "success_rate": (
                        round((successful_endpoints / total_endpoints * 100), 2)
                        if total_endpoints > 0
                        else 0
                    ),
                    "endpoint_results": result.results,
                }
                run_data["results"].append(file_result)

            results.append(run_data)

        return Response(
            {
                "period": f"Last {days} days",
                "total_test_runs": len(results),
                "test_runs": results,
            }
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Get a summary of health check performance
        """
        # Get query parameters
        days = request.query_params.get("days", 7)

        try:
            days = int(days)
        except ValueError:
            days = 7

        # Calculate date range
        from datetime import timedelta

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        # Get test runs in the period
        test_runs = APITestRun.objects.filter(
            started_at__gte=start_date, started_at__lte=end_date
        ).order_by("-started_at")

        # Calculate summary statistics
        total_runs = test_runs.count()
        successful_runs = test_runs.filter(status="finished").count()
        failed_runs = test_runs.filter(status="failed").count()
        running_runs = test_runs.filter(status="started").count()

        # Get all results for the period
        all_results = APITestResult.objects.filter(api_test_run__in=test_runs)

        # Calculate overall success rate
        total_endpoints_tested = 0
        total_successful_endpoints = 0

        for result in all_results:
            for endpoint_result in result.results:
                total_endpoints_tested += 1
                if endpoint_result.get("status") == "up":
                    total_successful_endpoints += 1

        overall_success_rate = (
            round((total_successful_endpoints / total_endpoints_tested * 100), 2)
            if total_endpoints_tested > 0
            else 0
        )

        # Get file-wise statistics
        file_stats = {}
        for result in all_results:
            file_name = result.file
            if file_name not in file_stats:
                file_stats[file_name] = {
                    "total_tests": 0,
                    "successful_tests": 0,
                    "failed_tests": 0,
                }

            for endpoint_result in result.results:
                file_stats[file_name]["total_tests"] += 1
                if endpoint_result.get("status") == "up":
                    file_stats[file_name]["successful_tests"] += 1
                else:
                    file_stats[file_name]["failed_tests"] += 1

        # Calculate success rates for each file
        for file_name, stats in file_stats.items():
            stats["success_rate"] = (
                round((stats["successful_tests"] / stats["total_tests"] * 100), 2)
                if stats["total_tests"] > 0
                else 0
            )

        return Response(
            {
                "period": f"Last {days} days",
                "summary": {
                    "total_test_runs": total_runs,
                    "successful_runs": successful_runs,
                    "failed_runs": failed_runs,
                    "running_runs": running_runs,
                    "run_success_rate": (
                        round((successful_runs / total_runs * 100), 2)
                        if total_runs > 0
                        else 0
                    ),
                    "total_endpoints_tested": total_endpoints_tested,
                    "total_successful_endpoints": total_successful_endpoints,
                    "overall_endpoint_success_rate": overall_success_rate,
                },
                "file_statistics": file_stats,
                "recent_runs": [
                    {
                        "id": run.id,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                        "status": run.status,
                    }
                    for run in test_runs[:5]  # Last 5 runs
                ],
            }
        )

    @action(detail=True, methods=["get"])
    def details(self, request, pk=None):
        """
        Get detailed results for a specific test run
        """
        try:
            test_run = APITestRun.objects.get(pk=pk)
            results = APITestResult.objects.filter(api_test_run=test_run)

            detailed_results = []
            for result in results:
                # Calculate statistics for this file
                total_endpoints = len(result.results)
                successful_endpoints = sum(
                    1 for r in result.results if r.get("status") == "up"
                )
                failed_endpoints = total_endpoints - successful_endpoints

                file_detail = {
                    "file": result.file,
                    "checked_at": result.checked_at,
                    "statistics": {
                        "total_endpoints": total_endpoints,
                        "successful_endpoints": successful_endpoints,
                        "failed_endpoints": failed_endpoints,
                        "success_rate": (
                            round((successful_endpoints / total_endpoints * 100), 2)
                            if total_endpoints > 0
                            else 0
                        ),
                    },
                    "endpoint_results": result.results,
                }
                detailed_results.append(file_detail)

            return Response(
                {
                    "test_run": {
                        "id": test_run.id,
                        "started_at": test_run.started_at,
                        "finished_at": test_run.finished_at,
                        "status": test_run.status,
                    },
                    "results": detailed_results,
                }
            )

        except APITestRun.DoesNotExist:
            return Response({"error": "Test run not found"}, status=404)
