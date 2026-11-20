from dataclasses import dataclass


@dataclass
class APIError(Exception):
    status: int
    code: str
    message: str
    retryable: bool = False
