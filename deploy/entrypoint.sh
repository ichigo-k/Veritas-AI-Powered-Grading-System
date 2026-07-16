#!/bin/sh

echo "Starting SQS grading worker..."
python manage.py run_grading_worker &

echo "Starting gunicorn..."
exec gunicorn verion_ai_grader.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${GUNICORN_WORKERS:-1}" \
  --timeout "${GUNICORN_TIMEOUT:-1800}" \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --max-requests 1000 \
  --max-requests-jitter 50 \
  --access-logfile - \
  --error-logfile -
