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
    text = str(text)
    text = re.sub(r'(?i)(?:sk-|ccr-profile-)[A-Za-z0-9_-]+', '[REDACTED]', text)
    text = re.sub(r'(?im)((?:authorization|cookie)\s*:\s*)[^\r\n]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)("(?:api[_-]?key|access_token|auth_token|token|password|cookie|authorization)"\s*:\s*)"(?:\\.|[^"\\])*"',
                  r'\1"[REDACTED]"', text)
    text = re.sub(r'(?i)((?:api[_-]?key|token|password)\s*[=:]\s*)[^\s&,;"}]+', r'\1[REDACTED]', text)
    # CLI / quoted secret flags before any display slicing.
    text = re.sub(
        r'''(?i)(--?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|authorization))(\s+|=)([^\s"']+|"[^"\n]*"|'[^'\n]*')''',
        r'\1\2[REDACTED]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'''(?i)(?:https?|wss?|ftp)://[^\s<>"']+@[^\s<>"']+''', '[URL]', text)
    return text
