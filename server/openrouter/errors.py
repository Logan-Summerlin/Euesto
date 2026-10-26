# A provider rejected the requested ``reasoning.effort`` (HTTP 400 naming it); the run
# retries once one level lower.
REASONING_EFFORT_REJECTED = "provider.reasoning_effort_rejected"


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
