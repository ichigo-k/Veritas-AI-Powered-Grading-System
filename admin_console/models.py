class _Manager:
    def create(self, **kwargs): return RequestMetric(**kwargs)
class RequestMetric:
    STATUS_RUNNING='RUNNING'; STATUS_ERROR='ERROR'; STATUS_SUCCESS='SUCCESS'; objects=_Manager()
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
    def save(self, **kwargs): pass
