# production.py
from .common import *

# Engineering preview stays staging-only even if staging flags are copied into
# the production environment. Keep legacy catalogs and BoM behavior available.
PROJECT_WORKSTATION_COMPOSITION_ENABLED = False
PROCESS_ASSEMBLY_BOM_ENABLED = False
PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED = False
AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED = False
AI_ACCEPTANCE_ACTION_ITEMS_ENABLED = False
ACCEPTANCE_CHECKLISTS_ENABLED = False
PROJECT_DOCUMENT_GENERATION_ENABLED = False
PROJECT_DOCUMENT_AI_ENABLED = False
AAS_ADVANCED_EDITOR_ENABLED = False
AAS_EXTERNAL_WRITE_ENABLED = False
AAS_SEMANTIC_PROFILE_UI_ENABLED = False

DEBUG = True

ALLOWED_HOSTS = ["*"]

CORS_ALLOW_ALL_ORIGINS = True
# CORS_ORIGIN_WHITELIST = [
#     'https://cpm.sharemundolinks.com',
#     'https://core.sharemundolinks.com',
#     'https://basic.sharemundolinks.com',
# ]

# Production Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST"),
        "PORT": env("DB_PORT"),
    }
}
STORAGES = {
    'staticfiles': {
        'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
    },
    'default': {
        'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
    }
}

# STATICFILES_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'

STATIC_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/static/'
STATIC_ROOT = 'staticfiles/'  
MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/media/'
MEDIA_ROOT = 'media/' 

EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
EMAIL_PORT = 587
EMAIL_HOST = env('EMAIL_HOST', default='email-smtp.eu-west-3.amazonaws.com')
EMAIL_HOST_USER = env('EMAIL_HOST_USER')
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="info@sharemundolinks.com")
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD')

AWS_SQS_QUEUE_URL = env('AWS_SQS_QUEUE_URL', default="https://sqs.eu-west-3.amazonaws.com/024848485297/calendar-reminder-queue")
AWS_SQS_REGION_NAME = env('AWS_SQS_REGION_NAME', default="eu-west-3")
AWS_SQS_QUEUE_ARN = env('AWS_SQS_QUEUE_ARN', default="arn:aws:sqs:eu-west-3:024848485297:calendar-reminder-queue")
AWS_SQS_ROLE_ARN = env('AWS_SQS_ROLE_ARN', default="arn:aws:iam::024848485297:role/service-role/Amazon_EventBridge_Scheduler_SQS_44d591e030")

HEALTH_CHECK_AUTH0_USERNAME = env("HEALTH_CHECK_AUTH0_USERNAME", default="arslan.haider+prodadmin@folium.ai")
HEALTH_CHECK_AUTH0_PASSWORD = env("HEALTH_CHECK_AUTH0_PASSWORD", default="Share11@")
AUTH0_CONNECTION = env("AUTH0_CONNECTION", default="PRODUCTION-DATABASE")
