import base64
import hashlib

def encode_token(file_id: str) -> str:
    raw = file_id.encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def decode_token(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode()

def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]
