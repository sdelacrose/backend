from rest_framework.exceptions import APIException

class InactiveLicenseException(APIException):
    status_code = 423
    default_detail = "User does not have an active license for this product."
    default_code = "inactive_license"

class ProjectAccessException(APIException):
    status_code = 403
    default_detail = "You cannot modify the project information - please contact the Project Creator."
    default_code = "project_access"
