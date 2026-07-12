"""SQS integration for asynchronous assessment grading."""

import boto3
from django.conf import settings


def _client():
    kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        kwargs.update(
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
    return boto3.client("sqs", **kwargs)


def enqueue_assessment(assessment_id: int) -> str:
    response = _client().send_message(
        QueueUrl=settings.SQS_QUEUE_URL,
        MessageBody=str(assessment_id),
        MessageAttributes={
            "job_type": {"DataType": "String", "StringValue": "assessment_grading"},
        },
    )
    return response["MessageId"]


def receive_messages():
    return _client().receive_message(
        QueueUrl=settings.SQS_QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=settings.SQS_WAIT_TIME_SECONDS,
        VisibilityTimeout=settings.SQS_VISIBILITY_TIMEOUT,
    ).get("Messages", [])


def delete_message(receipt_handle: str) -> None:
    _client().delete_message(
        QueueUrl=settings.SQS_QUEUE_URL, ReceiptHandle=receipt_handle
    )
