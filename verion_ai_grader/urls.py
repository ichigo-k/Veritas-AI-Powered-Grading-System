"""
URL configuration for verion_ai_grader project.
"""

from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.permissions import AllowAny

urlpatterns = [
    path('', RedirectView.as_view(pattern_name='admin_console:dashboard', permanent=False), name='home'),
    path('console/', include('admin_console.urls')),
    path('', include('grader.urls')),

    # API docs — public, no API key required to browse
    path('api/schema/', SpectacularAPIView.as_view(permission_classes=[AllowAny]), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema', permission_classes=[AllowAny]), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema', permission_classes=[AllowAny]), name='redoc'),
]
