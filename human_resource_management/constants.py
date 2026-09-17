from datetime import date
from ecpmpapp.utils import get_frontend_url_for_solution, get_user_active_solution

# Constants for leave calculations
START_YEAR = date.today().year
END_YEAR = date.today().year

EXCEL_MIME_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

def approval_link(manager) -> str:
    return get_frontend_url_for_solution(get_user_active_solution(manager.id))

def full_approval_link(manager) -> str:
    return f"{approval_link(manager)}/human-resource"
