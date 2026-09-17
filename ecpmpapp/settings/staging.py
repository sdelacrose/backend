# staging.py
from .common import *

DEBUG = True

# ALLOWED_HOSTS = [
#     'acc26bf2691cf485f8c9bc739c03745f-1317841195.eu-west-3.elb.amazonaws.com',
#     'sharemundo-backend.foliumaitech.com',
#     'staging-ecpmpfrontend-sharemundo.vercel.app',
#     '13.36.141.90',
#     '13.39.201.197',
#     '15.237.236.176',
#     '13.38.243.1',
#     '15.237.222.48',
#     '15.188.45.134',
#     'http://k8s-default-ecpmpapp-3886496305-1562241282.eu-west-3.elb.amazonaws.com'

# ]
ALLOWED_HOSTS = ['*']



CORS_ALLOW_ALL_ORIGINS = True

# Staging Database
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

SWAGGER_SETTINGS = {
    'DEFAULT_API_URL': 'https://sharemundo-backend.foliumaitech.com/api',
    'SECURITY_DEFINITIONS': {
        'Bearer': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header',
        }
    },
}

# staging settings
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

HEALTH_CHECK_AUTH0_USERNAME = env("HEALTH_CHECK_AUTH0_USERNAME", default="arslan.haider+company@folium.ai")
HEALTH_CHECK_AUTH0_PASSWORD = env("HEALTH_CHECK_AUTH0_PASSWORD", default="Share11@")
AUTH0_CONNECTION = env("AUTH0_CONNECTION", default="Username-Password-Authentication")
