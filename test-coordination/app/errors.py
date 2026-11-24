from __future__ import annotations


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, details: list[str] | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []
        if status_code:
            self.status_code = status_code


class NotFound(DomainError):
    status_code = 404


class Forbidden(DomainError):
    status_code = 403


class Conflict(DomainError):
    status_code = 409


class Unprocessable(DomainError):
    status_code = 422
