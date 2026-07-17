import logging


class MaxLevelFilter(logging.Filter):
    """Allow records up to and including the configured level."""

    def __init__(self, max_level: str = "INFO") -> None:
        super().__init__()
        self.max_level = int(getattr(logging, max_level.upper()))

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level
