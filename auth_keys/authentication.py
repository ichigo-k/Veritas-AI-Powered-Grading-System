import hashlib

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import ApiKey


class ApiKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        api_key_value = request.META.get('HTTP_X_API_KEY')
        if not api_key_value:
            return None

        key_hash = hashlib.sha256(api_key_value.encode('utf-8')).hexdigest()

        try:
            api_key = ApiKey.objects.get(key_hash=key_hash, is_active=True)
        except ApiKey.DoesNotExist:
            raise AuthenticationFailed('Invalid or inactive API key.')

        return (api_key, None)

    def authenticate_header(self, request) -> str:
        return 'X-API-Key'


class ApiKeyAuthenticationExtension(OpenApiAuthenticationExtension):
    target_class = 'auth_keys.authentication.ApiKeyAuthentication'
    name = 'ApiKeyAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'apiKey',
            'in': 'header',
            'name': 'X-API-Key',
        }
