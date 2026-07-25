import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.config import Settings
from app.main import root, scheduling_host_allows_path
from app.routes.auth import google_auth_start
from app.routes.calendar import _calendar_result_redirect
from app.routes.scheduling import MANAGEMENT_HTML, PUBLIC_HTML, scheduling_management_page


def _request(
    *,
    session: dict[str, object] | None = None,
    host: str = "lessons.example.com",
) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", host.encode())],
            "server": (host, 443),
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

    def test_management_layout_explains_commute_and_start_time_increments(self) -> None:
        booking_position = MANAGEMENT_HTML.index("<h2>Booking page</h2>")
        availability_position = MANAGEMENT_HTML.index("<h2>Weekly availability</h2>")
        lesson_types_position = MANAGEMENT_HTML.index("<h2>Lesson types</h2>")

        self.assertLess(booking_position, availability_position)
        self.assertLess(availability_position, lesson_types_position)
        self.assertIn("Commute time before a lesson (minutes)", MANAGEMENT_HTML)
        self.assertIn("Start-time increments (minutes)", MANAGEMENT_HTML)
        self.assertIn("Lessons can be booked back-to-back.", MANAGEMENT_HTML)
        self.assertIn('class="card availability-card"', MANAGEMENT_HTML)
        self.assertNotIn('class="card wide"', MANAGEMENT_HTML)
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", MANAGEMENT_HTML)
        self.assertIn("'Saturday','Sunday'", MANAGEMENT_HTML)
        self.assertIn("Disable any day you do not teach.", MANAGEMENT_HTML)
        self.assertIn("className='day-toggle'", MANAGEMENT_HTML)
        self.assertNotIn("remove.textContent='Remove'", MANAGEMENT_HTML)
        self.assertNotIn("Family Copilot", MANAGEMENT_HTML)
        self.assertNotIn(">Dashboard<", MANAGEMENT_HTML)
        self.assertIn('id="google-accounts"', MANAGEMENT_HTML)
        self.assertIn("No Google account connected", MANAGEMENT_HTML)
        self.assertIn("No calendars discovered", MANAGEMENT_HTML)
        self.assertIn("result.sync_warning", MANAGEMENT_HTML)
        self.assertIn("Google Calendar connected and calendars loaded.", MANAGEMENT_HTML)
        self.assertIn("calendars could not be loaded", MANAGEMENT_HTML)

    def test_public_booking_uses_calendar_layout_and_has_no_eyebrow(self) -> None:
        self.assertIn("width:min(1100px,100%)", PUBLIC_HTML)
        self.assertIn('grid-template-columns:260px minmax(0,1fr)', PUBLIC_HTML)
        self.assertIn('grid-template-columns:repeat(7,minmax(36px,1fr))', PUBLIC_HTML)
        self.assertIn('<header class="page-header"><h1 id="title">', PUBLIC_HTML)
        self.assertIn('id="calendar"', PUBLIC_HTML)
        self.assertIn('id="previous-month"', PUBLIC_HTML)
        self.assertIn('id="next-month"', PUBLIC_HTML)
        self.assertIn("linear-gradient(135deg,var(--indigo),var(--violet))", PUBLIC_HTML)
        self.assertIn("--indigo:#5f72dc", PUBLIC_HTML)
        self.assertIn("--violet:#8b5bd6", PUBLIC_HTML)
        self.assertIn('class="step-number">1</span>', PUBLIC_HTML)
        self.assertIn('class="step-number">2</span>', PUBLIC_HTML)
        self.assertIn("profile.booking_window_days", PUBLIC_HTML)
        self.assertNotIn("end.setDate(end.getDate()+13)", PUBLIC_HTML)
        self.assertNotIn('<p class="muted">Lesson scheduling</p>', PUBLIC_HTML)

    async def test_authenticated_lessons_root_opens_management(self) -> None:
        with patch("app.main.get_settings", return_value=self.settings):
            response = await root(_request(session={"google_email": "tutor@example.com"}))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/schedule/manage")

    async def test_management_page_has_no_family_dashboard_link(self) -> None:
        response = await scheduling_management_page(
            _request(session={"google_email": "tutor@example.com"})
        )

        body = response.body.decode()
        self.assertNotIn("Family Copilot", body)
        self.assertNotIn(">Dashboard<", body)

    def test_lessons_host_exposes_only_scheduling_and_required_login_routes(self) -> None:
        self.assertTrue(scheduling_host_allows_path("/schedule/manage"))
        self.assertTrue(scheduling_host_allows_path("/book/oksana-erm"))
        self.assertTrue(scheduling_host_allows_path("/api/scheduling/manage"))
        self.assertTrue(scheduling_host_allows_path("/calendar/google/start"))
        self.assertFalse(scheduling_host_allows_path("/dashboard"))
        self.assertFalse(scheduling_host_allows_path("/api/dashboard"))
        self.assertFalse(scheduling_host_allows_path("/api/tasks/day"))

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
