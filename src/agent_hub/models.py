import re

TERMINAL = frozenset({'succeeded', 'failed', 'timed_out', 'cancelled',
                      'interrupted', 'delivery_unknown'})


class HubError(Exception):
    def __init__(self, code, message, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def payload(self):
        from .store import Store
        return {"code": self.code, "message": redact(str(self)),
                "retryable": False, "details": Store.safe(self.details)}


def redact(text):
    text = re.sub(r'(?i)(?:sk-|ccr-profile-)[A-Za-z0-9_-]+', '[REDACTED]', str(text))
    text = re.sub(r'(?im)((?:authorization|cookie)\s*:\s*)[^\r\n]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)("(?:api[_-]?key|access_token|auth_token|token|password|cookie|authorization)"\s*:\s*)"(?:\\.|[^"\\])*"',
                  r'\1"[REDACTED]"', text)
    return re.sub(r'(?i)((?:api[_-]?key|token|password)\s*[=:]\s*)[^\s&,;"}]+', r'\1[REDACTED]', text)
