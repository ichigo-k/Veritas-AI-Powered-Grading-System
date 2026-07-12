import logging

from django.core.management.base import BaseCommand

from grader.services import GraderService
from grader.sqs import delete_message, receive_messages

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Consume assessment grading jobs from SQS."

    def handle(self, *args, **options):
        self.stdout.write("Grading worker started")
        while True:
            for message in receive_messages():
                assessment_id = int(message["Body"])
                try:
                    logger.info("SQS grading job started: assessment_id=%d", assessment_id)
                    GraderService().grade_assessment(assessment_id)
                    delete_message(message["ReceiptHandle"])
                    logger.info("SQS grading job completed: assessment_id=%d", assessment_id)
                except Exception:
                    logger.exception(
                        "SQS grading job failed; leaving message for retry: assessment_id=%d",
                        assessment_id,
                    )
