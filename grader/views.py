"""
REST API views for the grader service.

POST /api/grade/assessment/{assessment_id}/  — batch grade all attempts
POST /api/grade/attempt/{attempt_id}/        — grade a single attempt
"""

import logging
import time

from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from grader.serializers import BatchGradingResultSerializer, SingleGradingResultSerializer

from grader.services import GraderService
from admin_console.models import RequestMetric
from admin_console.runtime import apply_shared_database_config

logger = logging.getLogger(__name__)


class AssessmentGradeView(APIView):
    """
    POST /api/grade/assessment/{assessment_id}/

    Grades all eligible (SUBMITTED or TIMED_OUT) attempts for the given assessment.
    Returns a summary with graded_count, grading_status, and plagiarism_flags.
    """

    @extend_schema(
        summary="Batch grade an assessment",
        description=(
            "Grades all SUBMITTED and TIMED_OUT attempts for the given assessment concurrently. "
            "Runs plagiarism detection across all answer hashes before grading. "
            "Adds subjective scores to the existing MCQ score on each attempt, "
            "writes the final score back to `assessment_attempts.score`, "
            "and sets `assessments.gradingStatus = GRADED`."
        ),
        request=None,
        parameters=[
            OpenApiParameter(
                name="assessment_id",
                location=OpenApiParameter.PATH,
                description="Primary key of the assessment to grade.",
                required=True,
                type=int,
            )
        ],
        responses={
            200: BatchGradingResultSerializer,
            401: OpenApiResponse(description="Missing or invalid X-API-Key header."),
            404: OpenApiResponse(description="Assessment not found."),
            500: OpenApiResponse(description="Unexpected internal error."),
        },
        tags=["Grading"],
    )
    def post(self, request: Request, assessment_id: int) -> Response:
        metric = RequestMetric.objects.create(
            endpoint="assessment",
            target_id=assessment_id,
            status=RequestMetric.STATUS_RUNNING,
        )
        started = time.monotonic()
        service = GraderService()
        try:
            result = service.grade_assessment(assessment_id)
        except Http404 as exc:
            metric.status = RequestMetric.STATUS_ERROR
            metric.detail = str(exc)
            metric.finished_at = timezone.now()
            metric.duration_ms = int((time.monotonic() - started) * 1000)
            metric.save(update_fields=["status", "detail", "finished_at", "duration_ms"])
            return Response({"detail": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("Unexpected error grading assessment %d", assessment_id)
            metric.status = RequestMetric.STATUS_ERROR
            metric.detail = str(exc)[:1000]
            metric.finished_at = timezone.now()
            metric.duration_ms = int((time.monotonic() - started) * 1000)
            metric.save(update_fields=["status", "detail", "finished_at", "duration_ms"])
            return Response({"detail": "Internal server error."}, status=500)

        serializer = BatchGradingResultSerializer(result)
        metric.status = RequestMetric.STATUS_SUCCESS
        metric.detail = f"Graded {result.graded_count} attempt(s)."
        metric.finished_at = timezone.now()
        metric.duration_ms = int((time.monotonic() - started) * 1000)
        metric.save(update_fields=["status", "detail", "finished_at", "duration_ms"])
        return Response(serializer.data, status=200)


class AttemptGradeView(APIView):
    """
    POST /api/grade/attempt/{attempt_id}/

    Grades a single attempt and returns per-answer feedback.
    """

    @extend_schema(
        summary="Grade a single attempt",
        description=(
            "Grades a single attempt on demand. Useful for re-grading or targeted grading. "
            "Runs plagiarism detection against other attempts for the same assessment, "
            "calls AWS Bedrock per subjective answer, and writes the final score back to "
            "`assessment_attempts.score`."
        ),
        request=None,
        parameters=[
            OpenApiParameter(
                name="attempt_id",
                location=OpenApiParameter.PATH,
                description="Primary key of the attempt to grade.",
                required=True,
                type=int,
            )
        ],
        responses={
            200: SingleGradingResultSerializer,
            401: OpenApiResponse(description="Missing or invalid X-API-Key header."),
            404: OpenApiResponse(description="Attempt not found."),
            500: OpenApiResponse(description="Unexpected internal error."),
        },
        tags=["Grading"],
    )
    def post(self, request: Request, attempt_id: int) -> Response:
        metric = RequestMetric.objects.create(
            endpoint="attempt",
            target_id=attempt_id,
            status=RequestMetric.STATUS_RUNNING,
        )
        started = time.monotonic()
        service = GraderService()
        try:
            result = service.grade_attempt(attempt_id)
        except Http404 as exc:
            metric.status = RequestMetric.STATUS_ERROR
            metric.detail = str(exc)
            metric.finished_at = timezone.now()
            metric.duration_ms = int((time.monotonic() - started) * 1000)
            metric.save(update_fields=["status", "detail", "finished_at", "duration_ms"])
            return Response({"detail": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("Unexpected error grading attempt %d", attempt_id)
            metric.status = RequestMetric.STATUS_ERROR
            metric.detail = str(exc)[:1000]
            metric.finished_at = timezone.now()
            metric.duration_ms = int((time.monotonic() - started) * 1000)
            metric.save(update_fields=["status", "detail", "finished_at", "duration_ms"])
            return Response({"detail": "Internal server error."}, status=500)

        serializer = SingleGradingResultSerializer(result)
        metric.status = RequestMetric.STATUS_SUCCESS
        metric.detail = f"Score {result.score}."
        metric.finished_at = timezone.now()
        metric.duration_ms = int((time.monotonic() - started) * 1000)
        metric.save(update_fields=["status", "detail", "finished_at", "duration_ms"])
        return Response(serializer.data, status=200)


class HealthCheckView(APIView):
    """
    GET /api/health/

    Lightweight health check — no authentication required.
    Returns 200 with service status. Used by the main system to verify
    the grader is reachable before triggering grading.
    """

    # No authentication for health checks
    authentication_classes = []
    permission_classes = []

    @extend_schema(
        summary="Health check",
        description="Returns 200 if the grader service is running and can reach the database.",
        responses={
            200: OpenApiResponse(description="Service is healthy."),
            503: OpenApiResponse(description="Service is unhealthy (database unreachable)."),
        },
        tags=["Health"],
    )
    def get(self, request: Request) -> Response:
        # Verify database connectivity with a lightweight query
        from django.db import connections
        try:
            apply_shared_database_config()
            conn = connections["neon"]
            conn.ensure_connection()
        except Exception as exc:
            logger.error("Health check failed: database unreachable — %s", exc)
            return Response(
                {"status": "unhealthy", "detail": "Database unreachable"},
                status=503,
            )

        return Response({"status": "ok"}, status=200)
