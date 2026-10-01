from __future__ import annotations

from app.providers.base import ErrorClass

MAX_MESSAGE_CHARS = 300


class ProviderError(Exception):
    def __init__(
        self,
        provider: str,
        error_class: ErrorClass,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retry_after_s: float | None = None,
        request_id: str | None = None,
    ) -> None:
        self.provider = provider
        self.error_class = error_class
        self.message = message[:MAX_MESSAGE_CHARS]
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after_s = retry_after_s
        self.request_id = request_id
        super().__init__(provider, error_class, self.message)

    @property
    def retry_after_ms(self) -> int | None:
        if self.retry_after_s is None:
            return None
        return round(self.retry_after_s * 1000)

    def __str__(self) -> str:
        return (
            f"{self.provider}: {self.error_class} status={self.status_code} "
            f"code={self.error_code}: {self.message}"
        )

    def __repr__(self) -> str:
        return (
            f"ProviderError(provider={self.provider!r}, error_class={self.error_class!r}, "
            f"status_code={self.status_code!r}, error_code={self.error_code!r})"
        )


class MalformedResponseError(ProviderError):
    def __init__(self, provider: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(provider, ErrorClass.UNKNOWN, message, status_code=status_code)


class MissingCredentialError(ProviderError):
    def __init__(self, provider: str, env_name: str) -> None:
        super().__init__(provider, ErrorClass.AUTH_FAILED, f"credential missing: env {env_name}")
        self.env_name = env_name
