import logging

from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections

from grader.services import GraderService
from grader.sqs import delete_message, receive_messages

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Consume assessment grading jobs from SQS."

    def handle(self, *args, **options):
        self.stdout.write("Grading worker started")
        while True:
            # This loop runs for the lifetime of the process, unlike a normal
            # Django request cycle. Nothing else prunes connections that have
            # gone stale (closed server-side by Neon/a pooler/NAT) while idle,
            # so do it explicitly before each poll.
            close_old_connections()
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
                    # A dead connection doesn't self-repair — without closing it here,
                    # every subsequent message reuses the same broken connection and
                    # fails identically until the process restarts.
                    connections.close_all()
