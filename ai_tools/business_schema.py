from dataclasses import dataclass

from .serializers import TOOL_FILTERS


BUSINESS_QUERY_CONTRACT_VERSION = "6"


@dataclass(frozen=True)
class QueryOperation:
    tool_name: str
    service_name: str
    answer_type: str
    description: str


BUSINESS_QUERY_OPERATIONS = {
    "action_items": {
        "count": QueryOperation("action_items_count", "count_action_items", "count", "Count Action Items"),
        "list": QueryOperation("action_items_search", "search_action_items", "list", "List Action Items"),
        "biggest_delay": QueryOperation(
            "action_items_biggest_delay",
            "biggest_action_item_delay",
            "list",
            "Find the Action Item with the biggest delay",
        ),
        "group_by_project": QueryOperation(
            "action_items_group_by_project",
            "action_items_group_by_project",
            "chart",
            "Group Action Items by project",
        ),
        "group_by_milestone": QueryOperation(
            "action_items_group_by_milestone",
            "action_items_group_by_milestone",
            "chart",
            "Group project Action Items by milestone or phase",
        ),
        "financial_summary": QueryOperation(
            "action_items_financial_summary",
            "action_items_financial_summary",
            "summary",
            "Summarize Action Item budgets, costs, and hours",
        ),
        "closure_list": QueryOperation(
            "action_items_closure_list",
            "action_items_closure_list",
            "list",
            "List Action Items by closure date",
        ),
        "progress": QueryOperation(
            "action_items_progress",
            "action_items_progress_summary",
            "chart",
            "Summarize Action Item progress",
        ),
    },
    "documents": {
        "count": QueryOperation("documents_count", "count_documents", "count", "Count Documents"),
        "list": QueryOperation("documents_search", "search_documents", "list", "List Documents"),
        "group_by_status": QueryOperation(
            "documents_group_by_status",
            "documents_group_by_status",
            "chart",
            "Group Documents by status",
        ),
    },
    "object_assessments": {
        "count": QueryOperation(
            "object_assessments_count",
            "count_object_assessments",
            "count",
            "Count Object Assessment records",
        ),
        "list": QueryOperation(
            "object_assessments_search",
            "search_object_assessments",
            "list",
            "List Object Assessment records",
        ),
    },
    "calendar_events": {
        "count": QueryOperation(
            "calendar_count",
            "count_calendar",
            "count",
            "Count calendar events",
        ),
        "list": QueryOperation("calendar_search", "search_calendar", "list", "List calendar events"),
        "free_slot": QueryOperation(
            "calendar_free_slot",
            "find_free_calendar_slot",
            "availability",
            "Find a free calendar slot",
        ),
    },
    "projects": {
        "summary": QueryOperation("projects_summary", "projects_summary", "summary", "Summarize Projects"),
        "email_context": QueryOperation(
            "project_email_context",
            "project_email_context",
            "context",
            "Prepare project email context",
        ),
    },
    "project_members": {
        "list": QueryOperation(
            "project_team_members",
            "project_team_members",
            "list",
            "List visible Project team members",
        ),
    },
    "company_members": {
        "list": QueryOperation(
            "company_team_members",
            "company_team_members",
            "list",
            "List active users in the requester's company",
        ),
    },
    "demands": {
        "list": QueryOperation("matrix_search", "search_matrix_or_parts", "list", "List Customer Demands"),
        "uncovered": QueryOperation("matrix_gaps", "matrix_gap_analysis", "list", "List uncovered Customer Demands"),
    },
    "requirements": {
        "list": QueryOperation("matrix_search", "search_matrix_or_parts", "list", "List Technical Requirements"),
        "without_parts": QueryOperation(
            "matrix_gaps",
            "matrix_gap_analysis",
            "list",
            "List Technical Requirements without BOM Parts",
        ),
    },
    "bom_parts": {
        "list": QueryOperation("matrix_search", "search_matrix_or_parts", "list", "List Bill of Material Parts"),
    },
    "available_parts": {
        "list": QueryOperation("matrix_search", "search_matrix_or_parts", "list", "List Available Products"),
    },
    "purchase_orders": {
        "summary": QueryOperation(
            "purchase_orders_summary",
            "purchase_orders_summary",
            "summary",
            "Summarize Purchase Orders",
        ),
        "balance": QueryOperation(
            "purchase_order_balance",
            "purchase_order_balance",
            "summary",
            "Calculate Purchase Order invoice balance",
        ),
    },
    "invoices": {
        "list": QueryOperation("invoices_search", "search_invoices", "list", "List Invoices"),
    },
    "public_user_profiles": {
        "search": QueryOperation(
            "public_user_profile_search",
            "public_user_profile_search",
            "list",
            "Search public User Profiles",
        ),
    },
    "public_company_profiles": {
        "search": QueryOperation(
            "public_company_profile_search",
            "public_company_profile_search",
            "list",
            "Search public Company Profiles",
        ),
    },
    "public_profiles": {
        "search": QueryOperation(
            "public_profile_search",
            "public_profile_search",
            "list",
            "Search public User and Company Profiles",
        ),
    },
    "network": {
        "search": QueryOperation("network_search", "search_network", "list", "Search accepted Network connections"),
    },
}


ENTITY_DEFAULT_FILTERS = {
    "demands": {"entity": "demands"},
    "requirements": {"entity": "requirements"},
    "bom_parts": {"entity": "bom_parts"},
    "available_parts": {"entity": "available_parts"},
}

OPERATION_DEFAULT_FILTERS = {
    ("demands", "uncovered"): {"gap_type": "demands_without_requirements"},
    ("requirements", "without_parts"): {"gap_type": "requirements_without_parts"},
}


def public_business_schema():
    entities = {}
    for entity, operations in BUSINESS_QUERY_OPERATIONS.items():
        entities[entity] = {
            "operations": {
                name: {
                    "answer_type": operation.answer_type,
                    "description": operation.description,
                    "filters": sorted(TOOL_FILTERS.get(operation.tool_name) or []),
                }
                for name, operation in operations.items()
            }
        }
    return {
        "contract_version": BUSINESS_QUERY_CONTRACT_VERSION,
        "mode": "business_queries",
        "read_only": True,
        "entities": entities,
        "scope_types": ["company", "project", "accessible_projects", "all_accessible", "current_user"],
        "limits": {"default": 25, "maximum": 100},
        "notes": [
            "Document queries use metadata only and never search file contents.",
            "Object Assessment records are a separate entity from Documents.",
            "Every query is permission-filtered for the authenticated user.",
        ],
    }
