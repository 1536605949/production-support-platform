import json, logging, sys
from datetime import datetime, timezone

REDACT_KEYS = {'authorization','x-tool-api-key','api_key','token','password'}

def redact(value):
    if isinstance(value, dict):
        return {k: ('***' if k.lower() in REDACT_KEYS else redact(v)) for k,v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value

class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        extra = getattr(record, 'event', None)
        if extra:
            payload['event'] = redact(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)

def configure_logging(level='INFO'):
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root.addHandler(h)
    root.setLevel(level)
