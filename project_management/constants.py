from ecpmpapp.utils import get_frontend_url_for_solution

def frontend_url_project_milestone(solution) -> str:
    return get_frontend_url_for_solution(solution) + "/project-milestone-management"

def frontend_url_project_action_item(solution, action_item_id) -> str:
    return get_frontend_url_for_solution(solution) + f"/project-action-items?id={action_item_id}"

def frontend_url_company_action_item(solution, action_item_id) -> str:
    return get_frontend_url_for_solution(solution) + f"/company-action-items?id={action_item_id}"
