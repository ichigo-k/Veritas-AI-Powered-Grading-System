"""
Custom exceptions for the grading service.
"""

class GradingError(Exception):
    """
    Raised when a grading client (Bedrock, Ollama, etc.) fails to grade an answer.

    This includes:
    - API connection or timeout errors
    - JSON parse failure on the model response
    - Missing required fields in the model response
    """
    pass
