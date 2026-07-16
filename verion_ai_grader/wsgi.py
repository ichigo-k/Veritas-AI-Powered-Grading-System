import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'verion_ai_grader.settings')

application = get_wsgi_application()
