import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from starlette.requests import Request

from app.config import Settings
from app.routes.scheduling import (
    BugReportRequest,
    _feedback_attempts,
    submit_scheduling_bug_report,
)
from app.services.scheduling_feedback import (
    SchedulingFeedbackService,
    TurnstileValidationError,
    normalize_feedback_text,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        PUBLIC_BASE_URL="https://example.com",
        SCHEDULING_PUBLIC_BASE_URL="https://lessons.example.com",
        SCHEDULING_FEEDBACK_SMTP_USERNAME="sender@example.com",
        SCHEDULING_FEEDBACK_SMTP_APP_PASSWORD="test-app-password",
        SCHEDULING_FEEDBACK_TO_EMAIL="recipient@example.com",
        TURNSTILE_SITE_KEY="test-site-key",
        TURNSTILE_SECRET_KEY="test-secret-key",
    )


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/scheduling/bug-reports",
            "query_string": b"",
            "headers": [
                (b"host", b"lessons.example.com"),
                (b"origin", b"https://lessons.example.com"),
            ],
            "server": ("lessons.example.com", 443),
            "client": ("192.0.2.10", 12345),
            "session": {"google_email": "tutor@example.com"},
        }
    )


class SchedulingFeedbackServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_feedback_removes_control_characters(self) -> None:
        self.assertEqual(
            normalize_feedback_text("  Something\x00 broke\r\n\n\n\n\nPlease help.  "),
            "Something broke\n\n\nPlease help.",
        )

    async def test_turnstile_requires_expected_action_and_hostname(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "success": True,
            "action": "bug-report",
            "hostname": "lessons.example.com",
        }
        client = AsyncMock()
        client.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("app.services.scheduling_feedback.httpx.AsyncClient", return_value=context):
            await SchedulingFeedbackService(_settings()).verify_turnstile(
                token="valid-token",
                remote_ip="192.0.2.10",
                expected_hostname="lessons.example.com",
            )

        sent_data = client.post.await_args.kwargs["data"]
        self.assertEqual(sent_data["response"], "valid-token")
        self.assertEqual(sent_data["remoteip"], "192.0.2.10")

        response.json.return_value["action"] = "different-action"
        with (
            patch("app.services.scheduling_feedback.httpx.AsyncClient", return_value=context),
            self.assertRaises(TurnstileValidationError),
        ):
            await SchedulingFeedbackService(_settings()).verify_turnstile(
                token="valid-token",
                remote_ip=None,
                expected_hostname="lessons.example.com",
            )

    async def test_email_uses_fixed_headers_and_plain_text(self) -> None:
        service = SchedulingFeedbackService(_settings())
        with patch.object(service, "_send_message") as send_message:
            await service.send_bug_report(
                reporter_email="tutor@example.com",
                section="setup",
                message="The calendar button did not respond.",
            )

        email = send_message.call_args.args[0]
        self.assertEqual(email["Subject"], "[Tutor scheduling] Bug report")
        self.assertEqual(email["Reply-To"], "tutor@example.com")
        self.assertEqual(email.get_content_type(), "text/plain")
        self.assertIn("Dashboard section: setup", email.get_content())
        self.assertIn("The calendar button did not respond.", email.get_content())


class SchedulingFeedbackRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _feedback_attempts.clear()

    async def test_authenticated_report_is_verified_then_delivered(self) -> None:
        session = AsyncMock()

        @asynccontextmanager
        async def session_factory():
            yield session

        user = SimpleNamespace(google_email="tutor@example.com")
        service = Mock()
        service.configured = True
        service.verify_turnstile = AsyncMock()
        service.send_bug_report = AsyncMock()
        payload = BugReportRequest(
            section="lessons",
            message="Cancelling the lesson showed an unexpected error.",
            turnstile_token="valid-token",
        )

        with (
            patch("app.routes.scheduling.async_session_factory", session_factory),
            patch(
                "app.routes.scheduling._management_profile",
                new=AsyncMock(
                    return_value=(user, SimpleNamespace(), SimpleNamespace())
                ),
            ),
            patch("app.routes.scheduling.get_settings", return_value=_settings()),
            patch(
                "app.routes.scheduling.SchedulingFeedbackService",
                return_value=service,
            ),
        ):
            result = await submit_scheduling_bug_report(_request(), payload)

        self.assertEqual(result, {"sent": True})
        service.verify_turnstile.assert_awaited_once()
        service.send_bug_report.assert_awaited_once_with(
            reporter_email="tutor@example.com",
            section="lessons",
            message="Cancelling the lesson showed an unexpected error.",
        )

    async def test_honeypot_returns_success_without_sending(self) -> None:
        session = AsyncMock()

        @asynccontextmanager
        async def session_factory():
            yield session

        payload = BugReportRequest(
            section="other",
            message="Automated submission that should not be delivered.",
            turnstile_token="not-checked",
            website="https://spam.example",
        )
        with (
            patch("app.routes.scheduling.async_session_factory", session_factory),
            patch(
                "app.routes.scheduling._management_profile",
                new=AsyncMock(
                    return_value=(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
                ),
            ),
            patch("app.routes.scheduling.SchedulingFeedbackService") as service,
        ):
            result = await submit_scheduling_bug_report(_request(), payload)

        self.assertEqual(result, {"sent": True})
        service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
