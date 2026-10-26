from pathlib import Path
import os, environ

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

env = environ.Env()
environ.Env.read_env(".env.local")


for key, value in env.ENVIRON.items():
    os.environ[key] = value


SECRET_KEY = "django-insecure-8$0(ave=ku6neiu-oam@unkr*-)lhbvjy6k=)jo*_oipj^c%xm"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "ecpmpapp.permissions.LicensePermission"
    ],
    'DEFAULT_PARSER_CLASSES': (
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.FormParser',
        'rest_framework.parsers.MultiPartParser',
    ),
    "DEFAULT_PAGINATION_CLASS": "project_management.pagination.CustomPageNumberPagination",
    "PAGE_SIZE": 10,
    'COERCE_DECIMAL_TO_STRING': False, 
}

INSTALLED_APPS = [
    "corsheaders",
    "django.contrib.auth",
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "django_filters",
    "rest_framework",
    "drf_yasg",
    "storages",
    "simple_history",
    "channels",

    "models",
    "human_resource_management",
    "chat",
    "userapis",
    "companyprofileapis",
    "project_management",
    "ai_tools",
    "aas_integration",
    "project_content.apps.ProjectContentConfig",
    'notifications',
    'health_check'
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "ecpmpapp.csrf_middleware.DisableCSRFMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "ecpmpapp.jwt_token_middleware.BlockedTokenMiddleware",
    "ecpmpapp.middleware.CurrentRequestMiddleware",
]

TEMPLATES = [
    {
        "DIRS": [],
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "ecpmpapp.wsgi.application"
ASGI_APPLICATION = "ecpmpapp.asgi.application"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [(env("REDIS_HOST", default="redis"), 6379)],
        },
    },
}

# Matrix gateway configuration
MATRIX_ENABLED = env.bool("MATRIX_ENABLED", default=True)
MATRIX_BASE_URL = env("MATRIX_BASE_URL", default="http://matrix:8008")
MATRIX_ACCESS_TOKEN = env("MATRIX_ACCESS_TOKEN", default="")
MATRIX_REQUEST_TIMEOUT = env.int("MATRIX_REQUEST_TIMEOUT", default=10)
MATRIX_RETRY_ATTEMPTS = env.int("MATRIX_RETRY_ATTEMPTS", default=5)
MATRIX_READ_FROM_PROVIDER = env.bool("MATRIX_READ_FROM_PROVIDER", default=True)

# AI business data tool service authentication.
AI_TOOLS_SERVICE_API_KEY = env("AI_TOOLS_SERVICE_API_KEY", default="")
AI_TOOLS_SERVICE_API_KEY_HEADER = env("AI_TOOLS_SERVICE_API_KEY_HEADER", default="X-AI-Tools-Service-Key")

PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_URL = env(
    "PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_URL",
    default="http://localhost:8011",
)
PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY = env(
    "PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY",
    default="",
)
PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY_HEADER = env(
    "PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY_HEADER",
    default="X-Product-Assembly-Service-Key",
)
PRODUCT_ASSEMBLY_EXTRACTION_TIMEOUT_SECONDS = env.int(
    "PRODUCT_ASSEMBLY_EXTRACTION_TIMEOUT_SECONDS",
    default=180,
)
PROJECT_CONTENT_AI_TIMEOUT_SECONDS = env.int(
    "PROJECT_CONTENT_AI_TIMEOUT_SECONDS",
    default=180,
)
PROJECT_DOCUMENT_AI_TIMEOUT_SECONDS = env.int(
    "PROJECT_DOCUMENT_AI_TIMEOUT_SECONDS",
    default=330,
)
PROJECT_CONTENT_GENERATION_STALE_SECONDS = env.int(
    "PROJECT_CONTENT_GENERATION_STALE_SECONDS",
    default=300,
)
PRODUCT_ASSEMBLY_EXTRACTION_QUEUE = env(
    "PRODUCT_ASSEMBLY_EXTRACTION_QUEUE",
    default="product_assembly_extraction",
)
PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY = env.bool(
    "PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY",
    default=False,
)
PRODUCT_ASSEMBLY_EXTRACTION_MAX_SEGMENTS = env.int(
    "PRODUCT_ASSEMBLY_EXTRACTION_MAX_SEGMENTS",
    default=500,
)
PRODUCT_ASSEMBLY_EXTRACTION_MAX_CHARACTERS = env.int(
    "PRODUCT_ASSEMBLY_EXTRACTION_MAX_CHARACTERS",
    default=200000,
)

PROJECT_DOCUMENT_GENERATION_ENABLED = env.bool(
    "PROJECT_DOCUMENT_GENERATION_ENABLED",
    default=False,
)
PROJECT_DOCUMENT_AI_ENABLED = env.bool("PROJECT_DOCUMENT_AI_ENABLED", default=False)
ACCEPTANCE_CHECKLISTS_ENABLED = env.bool("ACCEPTANCE_CHECKLISTS_ENABLED", default=False)
AI_ACCEPTANCE_ACTION_ITEMS_ENABLED = env.bool(
    "AI_ACCEPTANCE_ACTION_ITEMS_ENABLED",
    default=False,
)
PROJECT_WORKSTATION_COMPOSITION_ENABLED = env.bool(
    "PROJECT_WORKSTATION_COMPOSITION_ENABLED",
    default=False,
)
PROCESS_ASSEMBLY_BOM_ENABLED = env.bool(
    "PROCESS_ASSEMBLY_BOM_ENABLED",
    default=False,
)
PROCESS_BOM_ASYNC_NODE_THRESHOLD = env.int(
    "PROCESS_BOM_ASYNC_NODE_THRESHOLD",
    default=500,
)
PROCESS_BOM_QUEUE = env("PROCESS_BOM_QUEUE", default="default")
PROCESS_BOM_RUNNING_TIMEOUT_SECONDS = env.int(
    "PROCESS_BOM_RUNNING_TIMEOUT_SECONDS",
    default=3600,
)
PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED = env.bool(
    "PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED",
    default=False,
)
AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED = env.bool(
    "AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED",
    default=False,
)
AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_TENANT_IDS = env.list(
    "AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_TENANT_IDS",
    default=[],
    cast=int,
)
PROJECT_AI_JOB_VIEW_ENABLED = env.bool("PROJECT_AI_JOB_VIEW_ENABLED", default=False)
FUNDING_OPPORTUNITY_MATCHING_ENABLED = env.bool(
    "FUNDING_OPPORTUNITY_MATCHING_ENABLED",
    default=False,
)
FUNDING_MATCH_THRESHOLD = env.int("FUNDING_MATCH_THRESHOLD", default=60)
FUNDING_SOURCE_FRESHNESS_DAYS = env.int("FUNDING_SOURCE_FRESHNESS_DAYS", default=30)
PROCESS_RECOMMENDATION_MINIMUM_SCORE = env(
    "PROCESS_RECOMMENDATION_MINIMUM_SCORE",
    default="0.85",
)
PROCESS_RECOMMENDATION_MINIMUM_MARGIN = env(
    "PROCESS_RECOMMENDATION_MINIMUM_MARGIN",
    default="0.15",
)
PROJECT_CONTENT_QUEUE = env("PROJECT_CONTENT_QUEUE", default="project_document_generation")
PROJECT_CONTENT_RUN_SYNCHRONOUSLY = env.bool(
    "PROJECT_CONTENT_RUN_SYNCHRONOUSLY",
    default=False,
)
PROJECT_CONTENT_MAX_SEGMENTS = env.int("PROJECT_CONTENT_MAX_SEGMENTS", default=500)
PROJECT_CONTENT_MAX_CHARACTERS = env.int(
    "PROJECT_CONTENT_MAX_CHARACTERS",
    default=200000,
)
PROJECT_CONTENT_TEMPLATE_MAX_BYTES = env.int(
    "PROJECT_CONTENT_TEMPLATE_MAX_BYTES",
    default=20 * 1024 * 1024,
)
PROJECT_CONTENT_TEMPLATE_MAX_UNCOMPRESSED_BYTES = env.int(
    "PROJECT_CONTENT_TEMPLATE_MAX_UNCOMPRESSED_BYTES",
    default=100 * 1024 * 1024,
)
PROJECT_CONTENT_TEMPLATE_MAX_ENTRIES = env.int(
    "PROJECT_CONTENT_TEMPLATE_MAX_ENTRIES",
    default=2000,
)
PROJECT_CONTENT_LIBREOFFICE_BINARY = env(
    "PROJECT_CONTENT_LIBREOFFICE_BINARY",
    default="soffice",
)
PROJECT_CONTENT_RENDER_TIMEOUT_SECONDS = env.int(
    "PROJECT_CONTENT_RENDER_TIMEOUT_SECONDS",
    default=30,
)

# ShareMundo AAS publication and Festo handover. External writes remain disabled in v1.
AAS_ENABLED = env.bool("AAS_ENABLED", default=False)
AAS_AUTO_PUBLISH_ENABLED = env.bool("AAS_AUTO_PUBLISH_ENABLED", default=False)
AAS_EXTERNAL_API_ENABLED = env.bool("AAS_EXTERNAL_API_ENABLED", default=False)
AAS_ADVANCED_EDITOR_ENABLED = env.bool("AAS_ADVANCED_EDITOR_ENABLED", default=False)
AAS_EXTERNAL_WRITE_ENABLED = env.bool("AAS_EXTERNAL_WRITE_ENABLED", default=False)
AAS_SEMANTIC_PROFILE_UI_ENABLED = env.bool(
    "AAS_SEMANTIC_PROFILE_UI_ENABLED",
    default=False,
)
AAS_GATEWAY_INTERNAL_URL = env("AAS_GATEWAY_INTERNAL_URL", default="http://localhost:8080/internal/basyx")
AAS_EXTERNAL_BASE_URL = env("AAS_EXTERNAL_BASE_URL", default="http://localhost:8080")
AAS_EXTERNAL_API_VERSION = env("AAS_EXTERNAL_API_VERSION", default="v3.1")
AAS_INTERNAL_SERVICE_KEY = env("AAS_INTERNAL_SERVICE_KEY", default="")
AAS_EDITOR_BASE_URL = env("AAS_EDITOR_BASE_URL", default="http://localhost:8080")
AAS_EDITOR_SESSION_KEY = env("AAS_EDITOR_SESSION_KEY", default="")
AAS_EDITOR_LAUNCH_TTL_SECONDS = env.int("AAS_EDITOR_LAUNCH_TTL_SECONDS", default=60)
AAS_REQUEST_TIMEOUT_SECONDS = env.int("AAS_REQUEST_TIMEOUT_SECONDS", default=15)
AAS_PUBLICATION_DEBOUNCE_SECONDS = env.int("AAS_PUBLICATION_DEBOUNCE_SECONDS", default=30)
AAS_PUBLICATION_RUN_SYNCHRONOUSLY = env.bool("AAS_PUBLICATION_RUN_SYNCHRONOUSLY", default=False)
AAS_PUBLICATION_QUEUE = env("AAS_PUBLICATION_QUEUE", default="aas_publication")
AASX_PREVIEW_MAX_COMPRESSED_BYTES = env.int(
    "AASX_PREVIEW_MAX_COMPRESSED_BYTES",
    default=50 * 1024 * 1024,
)
AASX_PREVIEW_MAX_DECOMPRESSED_BYTES = env.int(
    "AASX_PREVIEW_MAX_DECOMPRESSED_BYTES",
    default=200 * 1024 * 1024,
)
AASX_PREVIEW_MAX_ENTRIES = env.int("AASX_PREVIEW_MAX_ENTRIES", default=2000)
AASX_PREVIEW_MAX_COMPRESSION_RATIO = env.int(
    "AASX_PREVIEW_MAX_COMPRESSION_RATIO",
    default=100,
)
AASX_PREVIEW_SYNC_THRESHOLD_BYTES = env.int(
    "AASX_PREVIEW_SYNC_THRESHOLD_BYTES",
    default=10 * 1024 * 1024,
)
AASX_PREVIEW_CACHE_SECONDS = env.int("AASX_PREVIEW_CACHE_SECONDS", default=86400)

FESTO_HANDOVER_ENABLED = env.bool("FESTO_HANDOVER_ENABLED", default=False)
FESTO_PROJECT_LAUNCH_URL = env(
    "FESTO_PROJECT_LAUNCH_URL",
    default=env(
        "FESTO_PROJECT_API_URL",
        default="https://aas.festo.com/myfestodataspaceconnector/create-project",
    ),
)
FESTO_AAS_CLIENT_SUBJECT = env("FESTO_AAS_CLIENT_SUBJECT", default="local-festo-client")
FESTO_AAS_CLIENT_ID = env("FESTO_AAS_CLIENT_ID", default="")
FESTO_GRANT_TTL_SECONDS = env.int("FESTO_GRANT_TTL_SECONDS", default=86400)
FESTO_GRANT_MIN_EXPIRES_AT = env(
    "FESTO_GRANT_MIN_EXPIRES_AT",
    default="",
)
FESTO_CONFIRMATION_MAX_AGE_SECONDS = env.int("FESTO_CONFIRMATION_MAX_AGE_SECONDS", default=900)

# Chat push notifications (Firebase)
CHAT_PUSH_NOTIFICATIONS_ENABLED = env.bool("CHAT_PUSH_NOTIFICATIONS_ENABLED", default=True)
CHAT_PUSH_USE_CELERY = env.bool("CHAT_PUSH_USE_CELERY", default=False)
FIREBASE_PROJECT_ID = env("FIREBASE_PROJECT_ID", default="")
FIREBASE_CLIENT_EMAIL = env("FIREBASE_CLIENT_EMAIL", default="")
FIREBASE_PRIVATE_KEY = env("FIREBASE_PRIVATE_KEY", default="").replace("\\n", "\n")
FIREBASE_SERVICE_ACCOUNT_JSON = env("FIREBASE_SERVICE_ACCOUNT_JSON", default="")
FIREBASE_SERVICE_ACCOUNT_JSON_BASE64 = env("FIREBASE_SERVICE_ACCOUNT_JSON_BASE64", default="")
FIREBASE_WEB_PUSH_PUBLIC_VAPID_KEY = env(
    "FIREBASE_WEB_PUSH_PUBLIC_VAPID_KEY",
    default="BA75I8Yp5Lu2RPWGHzrhau3qmLfp_dE64JlsdsO16OHzInvlpyNgeGT-M9p5jMNDv0ShTwnpJrc4a8WRcQ4Ckx8",
)
FIREBASE_WEB_PUSH_PRIVATE_VAPID_KEY = env(
    "FIREBASE_WEB_PUSH_PRIVATE_VAPID_KEY",
    default="",
)

ROOT_URLCONF = "ecpmpapp.urls"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

SIMPLE_JWT = {
    'ALGORITHM': 'RS256',
    'AUDIENCE': env("AUTH0_AUDIENCE"),
    'ISSUER': f"{env('AUTH0_ISSUER_BASE_URL')}/",
    'JWK_URL': f"{env('AUTH0_ISSUER_BASE_URL')}/.well-known/jwks.json",
    'USER_ID_CLAIM': 'sub',
    'USER_ID_FIELD': 'auth0_sub',
    'JTI_CLAIM': None,
    'TOKEN_TYPE_CLAIM': None,
}

SWAGGER_SETTINGS = {
   'SECURITY_DEFINITIONS': {
        'Bearer': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header'
        }
    }
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'file': {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': 'debug.log',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['file'],
            'level': 'DEBUG',
            'propagate': True,
        },
    },
}

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'assets')
]

BACKEND_URL = env("BACKEND_URL", default="https://development.api.sharemundolinks.com/")
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:3000")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "models.User"
AUTH0_SECRET = env("AUTH0_SECRET")
AUTH0_DOMAIN = env("AUTH0_DOMAIN")
AUTH0_ISSUER_BASE_URL = env("AUTH0_ISSUER_BASE_URL")
AUTH0_CLIENT_ID = env("AUTH0_CLIENT_ID")
AUTH0_CLIENT_SECRET = env("AUTH0_CLIENT_SECRET")
AUTH0_M2M_CLIENT_ID = env("AUTH0_M2M_CLIENT_ID")
AUTH0_M2M_CLIENT_SECRET = env("AUTH0_M2M_CLIENT_SECRET")
AUTH0_AUDIENCE = env("AUTH0_AUDIENCE")
AUTH0_ALGORITHMS = ["RS256"]


AZURE_CLIENT_ID = env('AZURE_CLIENT_ID')
AZURE_CLIENT_SECRET = env('AZURE_CLIENT_SECRET')
AZURE_TENANT_ID = env('AZURE_TENANT_ID')
AZURE_REDIRECT_URI = f"{BACKEND_URL}api/project_management/azure/callback"


AWS_ACCESS_KEY_ID = env('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = env('AWS_SECRET_ACCESS_KEY')
AWS_STORAGE_BUCKET_NAME = env('AWS_STORAGE_BUCKET_NAME')
AWS_S3_REGION_NAME = 'eu-west-3'
AWS_S3_CUSTOM_DOMAIN = f'{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com'

AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None
AWS_LOCATION = 'media'

REDIS_HOST = env("REDIS_HOST", default="redis")
CELERY_BROKER_URL = f'redis://{REDIS_HOST}:6379/0'
CELERY_RESULT_BACKEND = f'redis://{REDIS_HOST}:6379/0'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'

DOCUMENT_INDEXING_ENABLED = env.bool("DOCUMENT_INDEXING_ENABLED", default=True)
DOCUMENT_OCR_ENABLED = env.bool("DOCUMENT_OCR_ENABLED", default=True)
DOCUMENT_CONTENT_SEARCH_ENABLED = env.bool(
    "DOCUMENT_CONTENT_SEARCH_ENABLED",
    default=True,
)
DOCUMENT_BACKFILL_ENABLED = env.bool("DOCUMENT_BACKFILL_ENABLED", default=False)
DOCUMENT_INDEXING_QUEUE = env("DOCUMENT_INDEXING_QUEUE", default="document_indexing")
DOCUMENT_INDEXING_MAX_FILE_SIZE_MB = env.int("DOCUMENT_INDEXING_MAX_FILE_SIZE_MB", default=100)
DOCUMENT_INDEXING_MAX_PAGES = env.int("DOCUMENT_INDEXING_MAX_PAGES", default=1000)
DOCUMENT_INDEXING_MAX_ATTACHMENTS = env.int("DOCUMENT_INDEXING_MAX_ATTACHMENTS", default=50)
DOCUMENT_INDEXING_TIMEOUT_SECONDS = env.int("DOCUMENT_INDEXING_TIMEOUT_SECONDS", default=900)
DOCUMENT_INDEXING_MAX_ATTEMPTS = env.int("DOCUMENT_INDEXING_MAX_ATTEMPTS", default=3)
DOCUMENT_INDEXING_PARSER_VERSION = env("DOCUMENT_INDEXING_PARSER_VERSION", default="document-search-v2")

ADVANCED_SEARCH_ENABLED = env.bool("ADVANCED_SEARCH_ENABLED", default=True)
ADVANCED_SEARCH_OBJECT_ASSESSMENTS = env.bool(
    "ADVANCED_SEARCH_OBJECT_ASSESSMENTS",
    default=True,
)
ADVANCED_SEARCH_BUSINESS_RECORDS = env.bool(
    "ADVANCED_SEARCH_BUSINESS_RECORDS",
    default=True,
)
ADVANCED_SEARCH_COMMENTS = env.bool("ADVANCED_SEARCH_COMMENTS", default=True)
ADVANCED_SEARCH_SEMANTIC_CATALOG = env.bool(
    "ADVANCED_SEARCH_SEMANTIC_CATALOG",
    default=True,
)
ADVANCED_SEARCH_BACKFILL_ENABLED = env.bool(
    "ADVANCED_SEARCH_BACKFILL_ENABLED",
    default=False,
)
ADVANCED_SEARCH_QUEUE = env("ADVANCED_SEARCH_QUEUE", default="business_search_indexing")
ADVANCED_SEARCH_MAX_ATTEMPTS = env.int("ADVANCED_SEARCH_MAX_ATTEMPTS", default=3)
ADVANCED_SEARCH_FUZZY_THRESHOLD = env.float(
    "ADVANCED_SEARCH_FUZZY_THRESHOLD",
    default=0.32,
)
ADVANCED_SEARCH_STATEMENT_TIMEOUT_MS = env.int(
    "ADVANCED_SEARCH_STATEMENT_TIMEOUT_MS",
    default=8000,
)
ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS = env.int(
    "ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS",
    default=4,
)
ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS_PER_USER = env.int(
    "ADVANCED_SEARCH_MAX_CONCURRENT_REQUESTS_PER_USER",
    default=1,
)
ADVANCED_SEARCH_REQUESTS_PER_MINUTE = env.int(
    "ADVANCED_SEARCH_REQUESTS_PER_MINUTE",
    default=30,
)
ADVANCED_SEARCH_CAPACITY_LEASE_SECONDS = env.int(
    "ADVANCED_SEARCH_CAPACITY_LEASE_SECONDS",
    default=60,
)
ADVANCED_SEARCH_CAPACITY_WAIT_SECONDS = env.float(
    "ADVANCED_SEARCH_CAPACITY_WAIT_SECONDS",
    default=3,
)
ADVANCED_SEARCH_CAPACITY_POLL_INTERVAL_MS = env.int(
    "ADVANCED_SEARCH_CAPACITY_POLL_INTERVAL_MS",
    default=100,
)

ENV = env("ENV", default="local")
PRODUCT_NAME = "Energy Consultant Solution"
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

API_SECRET_KEY = env('API_SECRET_KEY')
MS_CLIENT_ID = env('AZURE_CLIENT_ID')
MS_CLIENT_SECRET = env('AZURE_CLIENT_SECRET')
MS_TENANT_ID = env('AZURE_TENANT_ID')
MS_REDIRECT_URI=f"{BACKEND_URL}{env('MS_REDIRECT_URI')}"
# SECURITY-MAIL-0C: comma-separated Fernet key(s) for userapis/token_encryption.py, newest/
# primary key first (rotation support -- see that module's own docstring). Deliberately never
# SECRET_KEY, never hardcoded, never generated here. Optional and empty by default: unset is a
# valid "no encryption active yet" state -- historical plaintext Microsoft OAuth tokens in
# UserOutlookCredentials keep working either way (see userapis/signals.py).
MS_TOKEN_ENCRYPTION_KEYS = env("MS_TOKEN_ENCRYPTION_KEYS", default="")
# SECURITY-MAIL-0D: the explicit activation switch for write-side encryption (userapis/
# signals.py's pre_save receiver). False by default and NOT set in any environment file by
# this lot -- current behaviour (plaintext writes) stays unchanged until a deployment
# explicitly sets this to true alongside a valid MS_TOKEN_ENCRYPTION_KEYS.
MS_TOKEN_ENCRYPTION_WRITE_ENABLED = env.bool(
    "MS_TOKEN_ENCRYPTION_WRITE_ENABLED", default=False
)

# Health Check Configuration
HEALTH_CHECK_API_KEY = env("HEALTH_CHECK_API_KEY", default="health_check_secret_key_2024")

FRONTEND_URL_CPM = "{}cpm.sharemundolinks.com"
FRONTEND_URL_CORE = "{}core.sharemundolinks.com"
FRONTEND_URL_ENTERPRISE = "{}epm.sharemundolinks.com"
FRONTEND_URL_BACK_OFFICE = "bo.sharemundolinks.com"

if ENV == "local":
    FRONTEND_URL_CPM = env("FRONTEND_URL_CPM")
    FRONTEND_URL_CORE = env("FRONTEND_URL_CORE")
    FRONTEND_URL_ENTERPRISE = env("FRONTEND_URL_ENTERPRISE")
    FRONTEND_URL_BACK_OFFICE = "development.d1rq49at5kvgwq.amplifyapp.com"
elif ENV == "dev":
    FRONTEND_URL_CPM = FRONTEND_URL_CPM.format("development-")
    FRONTEND_URL_CORE = FRONTEND_URL_CORE.format("development-")
    FRONTEND_URL_ENTERPRISE = FRONTEND_URL_ENTERPRISE.format("development-")
    FRONTEND_URL_BACK_OFFICE = "development.d1rq49at5kvgwq.amplifyapp.com"
elif ENV == "staging":
    FRONTEND_URL_CPM = FRONTEND_URL_CPM.format("staging-")
    FRONTEND_URL_CORE = FRONTEND_URL_CORE.format("staging-")
    FRONTEND_URL_ENTERPRISE = FRONTEND_URL_ENTERPRISE.format("staging-")
    FRONTEND_URL_BACK_OFFICE = "stage.d1rq49at5kvgwq.amplifyapp.com"
elif ENV == "prod":
    FRONTEND_URL_CPM = FRONTEND_URL_CPM.format("")
    FRONTEND_URL_CORE = FRONTEND_URL_CORE.format("")
    FRONTEND_URL_ENTERPRISE = FRONTEND_URL_ENTERPRISE.format("")


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',  # Outputs to stdout
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',  # Or DEBUG, WARNING, etc.
    },
}
