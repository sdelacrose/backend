from django.db import connection
from django.conf import settings


DEFAULT_BILLING_GRACE_PERIOD_HOURS = 24


def get_billing_grace_period_hours():
    return int(
        getattr(
            settings,
            "PACKAGE_LICENSE_BILLING_GRACE_PERIOD_HOURS",
            DEFAULT_BILLING_GRACE_PERIOD_HOURS,
        )
    )


def get_active_license_sql_condition(package_alias="pkg", license_alias="l"):
    grace_hours = get_billing_grace_period_hours()
    return f"""
        {license_alias}.is_active = TRUE
        AND {package_alias}.is_active = TRUE
        AND {package_alias}.expires_at IS NOT NULL
        AND (
            {package_alias}.expires_at > NOW()
            OR (
                {package_alias}.status = 'Auto Renew'
                AND {package_alias}.is_freetrial = FALSE
                AND {package_alias}.stripe_subscription_id IS NOT NULL
                AND {package_alias}.expires_at > NOW() - INTERVAL '{grace_hours} hours'
            )
        )
    """


def get_active_license_user_ids_by_product_names(product_names):
    active_license_condition = get_active_license_sql_condition()
    query = f"""
        SELECT DISTINCT l.user_id
        FROM package_license_license AS l
        JOIN package_license_product AS p ON l.product_id = p.id
        JOIN package_license_package AS pkg ON l.package_id = pkg.id
        WHERE p.name = ANY(%s)
        AND {active_license_condition};
    """

    with connection.cursor() as cursor:
        cursor.execute(query, [product_names])
        return [row[0] for row in cursor.fetchall()]
