import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f"Required environment variable '{name}' is not set.")
    return value


BASE_DIR = Path(__file__).resolve().parent.parent

DATABASE_URL = os.environ.get('DATABASE_URL')
DJANGO_DB_URL = os.environ.get('DJANGO_DB_URL')

AI_PROVIDER = os.environ.get('AI_PROVIDER', 'bedrock').strip().lower()

if os.environ.get('USE_OLLAMA', 'False').lower() == 'true':
    AI_PROVIDER = 'ollama'

if AI_PROVIDER not in ('bedrock', 'ollama', 'gemini'):
    raise ImproperlyConfigured(
        f"AI_PROVIDER must be one of 'bedrock', 'ollama', 'gemini'; got '{AI_PROVIDER}'."
    )

USE_OLLAMA = AI_PROVIDER == 'ollama'

BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID')
OLLAMA_BASE_URL = None
OLLAMA_MODEL_ID = None
GEMINI_API_KEY = None
GEMINI_MODEL_ID = None

if AI_PROVIDER == 'ollama':
    OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434').strip('/')
    OLLAMA_MODEL_ID = os.environ.get('OLLAMA_MODEL_ID', 'llama3.1')
elif AI_PROVIDER == 'gemini':
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
    GEMINI_MODEL_ID = os.environ.get('GEMINI_MODEL_ID', 'gemini-2.5-flash')
else:
    BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'anthropic.claude-3-sonnet-20240229-v1:0')

OLLAMA_TIMEOUT = int(os.environ.get('OLLAMA_TIMEOUT', '300'))
OLLAMA_NUM_CTX = int(os.environ.get('OLLAMA_NUM_CTX', '4096'))
GEMINI_TIMEOUT = int(os.environ.get('GEMINI_TIMEOUT', '120'))

S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME')

if AI_PROVIDER == 'bedrock':
    _aws_required = True
else:
    _aws_required = bool(S3_BUCKET_NAME)

AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
SQS_QUEUE_URL = os.environ.get('SQS_QUEUE_URL', '')
SQS_WAIT_TIME_SECONDS = int(os.environ.get('SQS_WAIT_TIME_SECONDS', '20'))
SQS_VISIBILITY_TIMEOUT = int(os.environ.get('SQS_VISIBILITY_TIMEOUT', '1800'))

S3_UPLOAD_PREFIX = os.environ.get('S3_UPLOAD_PREFIX', 'grader-uploads').strip('/')
S3_PRESIGNED_URL_EXPIRES_IN = int(os.environ.get('S3_PRESIGNED_URL_EXPIRES_IN', '3600'))
AWS_S3_ENDPOINT_URL = os.environ.get('AWS_S3_ENDPOINT_URL') or None

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'dev-only-change-this-from-the-admin-console-deployment-environment',
)

DEBUG = os.environ.get('DEBUG', 'False') == 'True'

_allowed_hosts_env = os.environ.get('ALLOWED_HOSTS', '')
if _allowed_hosts_env:
    ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(',') if h.strip()]
elif DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    ALLOWED_HOSTS = []

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.staticfiles',
    'rest_framework',
    'drf_spectacular',
    'drf_spectacular_sidecar',
    'grader',
    'auth_keys',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'verion_ai_grader.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'verion_ai_grader.wsgi.application'

_django_db: dict
if DJANGO_DB_URL:
    _django_db = dj_database_url.parse(DJANGO_DB_URL, conn_max_age=600)
else:
    _django_db = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'django_system.db',
    }

_neon_db = (
    dj_database_url.parse(DATABASE_URL, conn_max_age=600)
    if DATABASE_URL
    else {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'unconfigured_shared.db',
    }
)

DATABASES = {
    'default': _django_db,
    'neon': _neon_db,
}

DATABASE_ROUTERS = ['grader.db_router.GraderRouter']

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'auth_keys.authentication.ApiKeyAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Verion AI Grader',
    'DESCRIPTION': (
        'AI grading microservice for the ai-powered-grading-system. '
        'Grades subjective answers using AWS Bedrock, detects plagiarism via '
        'answer hash comparison, and writes final scores back to the shared database.'
    ),
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SWAGGER_UI_DIST': 'SIDECAR',
    'SWAGGER_UI_FAVICON_HREF': 'SIDECAR',
    'REDOC_DIST': 'SIDECAR',
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {
            'format': '[{levelname}] {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'grader': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

BEDROCK_REQUEST_DELAY = float(os.environ.get('BEDROCK_REQUEST_DELAY', '3'))

_bedrock_max_tokens_raw = os.environ.get('BEDROCK_MAX_TOKENS', '2048')
try:
    BEDROCK_MAX_TOKENS: int = int(_bedrock_max_tokens_raw)
except ValueError as exc:
    raise ImproperlyConfigured(
        f"Environment variable 'BEDROCK_MAX_TOKENS' must be a valid integer, "
        f"got: '{_bedrock_max_tokens_raw}'"
    ) from exc

_grading_concurrency_raw = os.environ.get('GRADING_CONCURRENCY', '1')
try:
    GRADING_CONCURRENCY: int = int(_grading_concurrency_raw)
except ValueError as exc:
    raise ImproperlyConfigured(
        f"Environment variable 'GRADING_CONCURRENCY' must be a valid integer, "
        f"got: '{_grading_concurrency_raw}'"
    ) from exc
