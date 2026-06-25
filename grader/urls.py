"""
URL patterns for the grader app.
"""

from django.urls import path

from grader.views import AssessmentGradeView, AttemptGradeView, HealthCheckView

app_name = "grader"

urlpatterns = [
    path(
        "api/health/",
        HealthCheckView.as_view(),
        name="health-check",
    ),
    path(
        "api/grade/assessment/<int:assessment_id>/",
        AssessmentGradeView.as_view(),
        name="grade-assessment",
    ),
    path(
        "api/grade/attempt/<int:attempt_id>/",
        AttemptGradeView.as_view(),
        name="grade-attempt",
    ),
]
