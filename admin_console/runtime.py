def apply_shared_database_config(): pass
def get_runtime_int(name, default): return default
def get_provider_clients(): return []
def get_storage_config(): return None
class FallbackAIClient:
    def __init__(self, clients): self.client = clients[0]
    def grade_answer(self, **kwargs): return self.client.grade_answer(**kwargs)
