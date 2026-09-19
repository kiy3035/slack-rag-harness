import hashlib
import hmac
import time
from collections.abc import Callable


class SlackSignatureError(ValueError):
    """Slack 요청의 인증 정보를 신뢰할 수 없을 때 발생한다."""


class SlackConfigurationError(RuntimeError):
    """Slack 서명을 안전하게 검증할 설정이 없을 때 발생한다."""


class SlackSignatureVerifier:
    """원본 요청 본문과 Timestamp로 Slack HMAC 서명을 검증한다."""

    def __init__(
        self,
        signing_secret: str,
        tolerance_seconds: int = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Secret, 허용 시차, 교체 가능한 시계를 주입한다."""
        self._signing_secret = signing_secret.encode("utf-8")
        self._tolerance_seconds = tolerance_seconds
        self._clock = clock

    def verify(self, raw_body: bytes, timestamp: str | None, signature: str | None) -> None:
        """재전송 공격과 위조 요청을 파싱 전에 차단한다."""
        if not self._signing_secret:
            raise SlackConfigurationError("SLACK_SIGNING_SECRET_NOT_CONFIGURED")
        if not timestamp or not signature:
            raise SlackSignatureError("SLACK_SIGNATURE_MISSING")

        try:
            timestamp_bytes = timestamp.encode("ascii")
            request_time = int(timestamp)
        except (UnicodeEncodeError, ValueError) as exc:
            raise SlackSignatureError("SLACK_TIMESTAMP_INVALID") from exc

        if abs(self._clock() - request_time) > self._tolerance_seconds:
            raise SlackSignatureError("SLACK_TIMESTAMP_STALE")

        base_string = b"v0:" + timestamp_bytes + b":" + raw_body
        expected = "v0=" + hmac.new(
            self._signing_secret,
            base_string,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise SlackSignatureError("SLACK_SIGNATURE_INVALID")
