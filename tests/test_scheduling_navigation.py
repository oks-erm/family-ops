import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.config import Settings
from app.routes.auth import google_auth_start
from app.routes.calendar import _calendar_result_redirect
from app.routes.scheduling import scheduling_management_page


def _request(*, session: dict[str, object] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", b"lessons.example.com")],
            "server": ("lessons.example.com", 443),
            "session": session if session is not None else {},
        }
    )


class SchedulingNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            GOOGLE_CLIENT_ID="test-client-id",
            PUBLIC_BASE_URL="https://example.com",
            SCHEDULING_PUBLIC_BASE_URL="https://lessons.example.com",
        )

    async def test_management_login_preserves_scheduling_destination(self) -> None:
        response = await scheduling_management_page(_request())

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/google/start?next=scheduling")

    async def test_management_page_links_back_to_main_dashboard(self) -> None:
        with patch("app.routes.scheduling.get_settings", return_value=self.settings):
            response = await scheduling_management_page(
                _request(session={"google_email": "tutor@example.com"})
            )

        body = response.body.decode()
        self.assertIn('href="https://example.com/dashboard"', body)
        self.assertNotIn("__DASHBOARD_URL__", body)

    async def test_google_login_accepts_only_known_scheduling_destination(self) -> None:
        request = _request()
        with patch("app.routes.auth.get_settings", return_value=self.settings):
            response = await google_auth_start(request, next="scheduling")

        self.assertEqual(response.status_code, 307)
        self.assertEqual(request.session["oauth_next"], "scheduling")

        with patch("app.routes.auth.get_settings", return_value=self.settings):
            await google_auth_start(request, next="https://attacker.example")

        self.assertNotIn("oauth_next", request.session)

    async def test_calendar_result_returns_to_lessons_for_success_and_failure(self) -> None:
        for status in ("connected", "auth-failed"):
            request = _request(session={"calendar_oauth_next": "scheduling"})
            with patch("app.routes.calendar.get_settings", return_value=self.settings):
                response = _calendar_result_redirect(request, status)

            self.assertEqual(
                response.headers["location"],
                f"https://lessons.example.com/schedule/manage?calendar={status}",
            )
            self.assertNotIn("calendar_oauth_next", request.session)


if __name__ == "__main__":
    unittest.main()
