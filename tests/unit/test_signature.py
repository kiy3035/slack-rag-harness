import hashlib
import hmac

import pytest

from app.integrations.slack.signature import (
    SlackConfigurationError,
    SlackSignatureError,
    SlackSignatureVerifier,
)


def make_signature(secret: str, timestamp: str, body: bytes) -> str:
    """테스트 요청에 Slack v0 규격 서명을 생성한다."""
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def fixed_clock() -> float:
    """서명 Timestamp 경계 테스트를 위해 고정된 현재 시각을 반환한다."""
    return 1000.0


def test_signature_accepts_valid_raw_body() -> None:
    """원본 본문과 현재 Timestamp의 올바른 서명을 허용하는지 검증한다."""
    body = b'{"type":"url_verification","challenge":"ok"}'
    timestamp = "1000"
    verifier = SlackSignatureVerifier("secret", clock=fixed_clock)

    verifier.verify(body, timestamp, make_signature("secret", timestamp, body))


def test_signature_rejects_empty_signing_secret() -> None:
    """Secret 미설정 상태가 빈 키로 서명된 위조 요청을 허용하지 않는지 검증한다."""
    verifier = SlackSignatureVerifier("", clock=fixed_clock)

    with pytest.raises(SlackConfigurationError, match="SLACK_SIGNING_SECRET_NOT_CONFIGURED"):
        verifier.verify(b"{}", "1000", make_signature("", "1000", b"{}"))


@pytest.mark.parametrize(
    ("timestamp", "signature", "error_code"),
    [
        (None, None, "SLACK_SIGNATURE_MISSING"),
        ("not-a-number", "v0=bad", "SLACK_TIMESTAMP_INVALID"),
        ("600", "v0=bad", "SLACK_TIMESTAMP_STALE"),
        ("1000", "v0=bad", "SLACK_SIGNATURE_INVALID"),
    ],
)
def test_signature_rejects_invalid_authentication(
    timestamp: str | None,
    signature: str | None,
    error_code: str,
) -> None:
    """누락·형식 오류·오래된 시각·위조 서명을 각각 거절하는지 검증한다."""
    verifier = SlackSignatureVerifier("secret", clock=fixed_clock)

    with pytest.raises(SlackSignatureError, match=error_code):
        verifier.verify(b"{}", timestamp, signature)
