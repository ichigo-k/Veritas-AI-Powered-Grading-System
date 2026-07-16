from rest_framework import serializers


class CriteriaFeedbackSerializer(serializers.Serializer):
    criterion = serializers.CharField()
    awarded = serializers.IntegerField()
    max = serializers.IntegerField()
    justification = serializers.CharField()


class AnswerFeedbackSerializer(serializers.Serializer):
    question_id = serializers.IntegerField()
    total_score = serializers.FloatField()
    max_score = serializers.FloatField()
    flag = serializers.CharField()
    flag_reason = serializers.CharField()
    criteria_feedback = CriteriaFeedbackSerializer(many=True)
    bedrock_error = serializers.BooleanField()


class PlagiarismFlagSerializer(serializers.Serializer):
    question_id = serializers.IntegerField()
    flagged_attempt_ids = serializers.ListField(child=serializers.IntegerField())


class BatchGradingResultSerializer(serializers.Serializer):
    assessment_id = serializers.IntegerField()
    graded_count = serializers.IntegerField()
    grading_status = serializers.CharField()
    plagiarism_flags = PlagiarismFlagSerializer(many=True)


class SingleGradingResultSerializer(serializers.Serializer):
    attempt_id = serializers.IntegerField()
    score = serializers.FloatField()
    plagiarism_flagged = serializers.BooleanField()
    answer_feedbacks = AnswerFeedbackSerializer(many=True)
    error_notes = serializers.CharField(allow_blank=True)
