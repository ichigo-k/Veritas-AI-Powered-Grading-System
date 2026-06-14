"""
Django settings for verion_ai_grader project.
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """Return the value of a required environment variable or raise."""
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(
            f"Required environment variable '{name}' is not set."
        )
    return value


# ---------------------------------------------------------------------------
# Build paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Required environment variables
# ---------------------------------------------------------------------------

DATABASE_URL = _require_env('DATABASE_URL')
DJANGO_DB_URL = os.environ.get('DJANGO_DB_URL')  # optional — falls back to SQLite
AWS_ACCESS_KEY_ID = _require_env('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = _require_env('AWS_SECRET_ACCESS_KEY')
AWS_REGION = _require_env('AWS_REGION')
BEDROCK_MODEL_ID = _require_env('BEDROCK_MODEL_ID')
S3_BUCKET_NAME = _require_env('S3_BUCKET_NAME')
S3_UPLOAD_PREFIX = os.environ.get('S3_UPLOAD_PREFIX', 'grader-uploads').strip('/')
S3_PRESIGNED_URL_EXPIRES_IN = int(os.environ.get('S3_PRESIGNED_URL_EXPIRES_IN', '3600'))

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

SECRET_KEY = _require_env('DJANGO_SECRET_KEY')

DEBUG = os.environ.get('DEBUG', 'False') == 'True'

_allowed_hosts_env = os.environ.get('ALLOWED_HOSTS', '')
if _allowed_hosts_env:
    ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(',') if h.strip()]
elif DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    ALLOWED_HOSTS = []

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Database (split routing)
# ---------------------------------------------------------------------------
# default  → Django system DB: holds auth_*, contenttypes, auth_keys_apikey,
#            and django_migrations for those apps. Never touches the shared DB.
#            Defaults to a local SQLite file. Set DJANGO_DB_URL to use Postgres
#            (e.g. a separate schema or database in production).
# neon     → Shared PostgreSQL: grader app tables only (grader_gradingresult,
#            grader_answerfeedback). All managed=False models are also read/
#            written here but Django never runs migrations against them.
# ---------------------------------------------------------------------------

_django_db: dict
if DJANGO_DB_URL:
    _django_db = dj_database_url.parse(DJANGO_DB_URL, conn_max_age=600)
else:
    _django_db = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'django_system.db',
    }

DATABASES = {
    'default': _django_db,
    'neon': dj_database_url.config(default=DATABASE_URL, conn_max_age=600),
}

DATABASE_ROUTERS = ['grader.db_router.GraderRouter']

# ---------------------------------------------------------------------------
# Django REST Framework (task 1.6)
# ---------------------------------------------------------------------------

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
    # Use self-hosted Swagger/Redoc assets (no CDN dependency)
    'SWAGGER_UI_DIST': 'SIDECAR',
    'SWAGGER_UI_FAVICON_HREF': 'SIDECAR',
    'REDOC_DIST': 'SIDECAR',
}

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ---------------------------------------------------------------------------
# Optional environment variables with validation
# ---------------------------------------------------------------------------

# BEDROCK_MAX_TOKENS — integer, default 2048
_bedrock_max_tokens_raw = os.environ.get('BEDROCK_MAX_TOKENS', '2048')
try:
    BEDROCK_MAX_TOKENS: int = int(_bedrock_max_tokens_raw)
except ValueError as exc:
    raise ImproperlyConfigured(
        f"Environment variable 'BEDROCK_MAX_TOKENS' must be a valid integer, "
        f"got: '{_bedrock_max_tokens_raw}'"
    ) from exc

# GRADING_CONCURRENCY — integer, default 10
_grading_concurrency_raw = os.environ.get('GRADING_CONCURRENCY', '10')
try:
    GRADING_CONCURRENCY: int = int(_grading_concurrency_raw)
except ValueError as exc:
    raise ImproperlyConfigured(
        f"Environment variable 'GRADING_CONCURRENCY' must be a valid integer, "
        f"got: '{_grading_concurrency_raw}'"
    ) from exc
