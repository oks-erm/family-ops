import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.config import Settings
from app.main import root, scheduling_host_allows_path
from app.routes.auth import google_auth_start, logout, student_logout
from app.routes.calendar import _calendar_result_redirect
from app.routes.scheduling import (
    ACCOUNT_CONTROL_CSS,
    BUG_REPORT_HTML,
    MANAGEMENT_HTML,
    PUBLIC_HTML,
    PUBLIC_INFO_HTML,
    REGISTRATION_HTML,
    SELECTED_SUMMARY_HTML,
    STUDENT_HTML,
    TUTOR_LANDING_HTML,
    public_booking_page,
    scheduling_feedback_page,
    scheduling_management_page,
    student_lessons_page,
    tutor_landing_page,
)


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
        self.assertIn("Buffer before and after non-lesson events (minutes)", MANAGEMENT_HTML)
        self.assertNotIn("Commute time before a lesson", MANAGEMENT_HTML)
        self.assertNotIn("Commute time after a lesson", MANAGEMENT_HTML)
        self.assertIn("Start-time increments (minutes)", MANAGEMENT_HTML)
        self.assertIn("Lessons can still be booked back-to-back.", MANAGEMENT_HTML)
        self.assertIn('id="copy-public-link"', MANAGEMENT_HTML)
        self.assertIn('aria-label="Copy booking link"', MANAGEMENT_HTML)
        self.assertNotIn(">Copy link</button>", MANAGEMENT_HTML)
        self.assertIn("navigator.clipboard.writeText(state.profile.public_url)", MANAGEMENT_HTML)
        self.assertIn("Booking link copied.", MANAGEMENT_HTML)
        self.assertIn('id="completed-count"', MANAGEMENT_HTML)
        self.assertIn('id="monthly-count"', MANAGEMENT_HTML)
        self.assertIn("Total lessons this month", MANAGEMENT_HTML)
        self.assertNotIn("Completed + scheduled this month", MANAGEMENT_HTML)
        self.assertIn('id="earned-income"', MANAGEMENT_HTML)
        self.assertIn(
            "$('#earned-income').textContent=money(state.metrics.earned_cents)",
            MANAGEMENT_HTML,
        )
        self.assertIn('id="projected-income"', MANAGEMENT_HTML)
        self.assertNotIn("Paid lessons remaining", MANAGEMENT_HTML)
        self.assertIn('id="registered-payments"', MANAGEMENT_HTML)
        self.assertIn("/api/scheduling/student-payments/${x.id}", MANAGEMENT_HTML)
        self.assertIn("/api/scheduling/students/${encodeURIComponent(x.email)}", MANAGEMENT_HTML)
        self.assertIn("removeButton.innerHTML=trashIcon", MANAGEMENT_HTML)
        self.assertIn("remove.innerHTML=trashIcon", MANAGEMENT_HTML)
        self.assertIn("`Delete payment for ${x.student_email}`", MANAGEMENT_HTML)
        self.assertIn("known=['settings','lessons']", MANAGEMENT_HTML)
        self.assertIn('class="card availability-card"', MANAGEMENT_HTML)
        self.assertNotIn('class="card wide"', MANAGEMENT_HTML)
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", MANAGEMENT_HTML)
        self.assertIn(
            "grid-template-columns:minmax(0,1.1fr) minmax(420px,.9fr)",
            MANAGEMENT_HTML,
        )
        self.assertNotIn("minmax(0,1.35fr) minmax(340px,.75fr)", MANAGEMENT_HTML)
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
        self.assertIn("<h2>iCloud calendars</h2>", MANAGEMENT_HTML)
        self.assertIn('id="icloud"', MANAGEMENT_HTML)
        self.assertIn('name="app_specific_password"', MANAGEMENT_HTML)
        self.assertIn("never your main Apple password", MANAGEMENT_HTML)
        self.assertIn("/api/scheduling/icloud-connections", MANAGEMENT_HTML)
        self.assertIn('data-tab="lessons"', MANAGEMENT_HTML)
        self.assertIn('data-tab="settings"', MANAGEMENT_HTML)
        self.assertNotIn('data-tab="welcome"', MANAGEMENT_HTML)
        self.assertNotIn('id="welcome-panel"', MANAGEMENT_HTML)
        self.assertIn('id="settings-panel" hidden', MANAGEMENT_HTML)
        self.assertIn('id="lessons-panel"', MANAGEMENT_HTML)
        self.assertIn("result?'settings'", MANAGEMENT_HTML)
        self.assertIn("<h2>Students and balances</h2>", MANAGEMENT_HTML)
        self.assertIn('id="student-search"', MANAGEMENT_HTML)
        self.assertIn('id="student-count"', MANAGEMENT_HTML)
        self.assertIn("$('#student-count').textContent=state.students.length", MANAGEMENT_HTML)
        self.assertIn("<h2>All lessons</h2>", MANAGEMENT_HTML)
        for lesson_filter in ("all", "upcoming", "completed", "cancelled"):
            self.assertIn(f'data-lesson-filter="{lesson_filter}"', MANAGEMENT_HTML)
        self.assertIn("x.category===lessonFilter", MANAGEMENT_HTML)
        self.assertIn("if(x.category==='upcoming')", MANAGEMENT_HTML)
        self.assertIn("<h2>Register payment</h2>", MANAGEMENT_HTML)
        self.assertNotIn('id="upcoming-count"', MANAGEMENT_HTML)
        self.assertLess(
            MANAGEMENT_HTML.index('data-tab="lessons"'),
            MANAGEMENT_HTML.index('data-tab="settings"'),
        )
        self.assertLess(
            MANAGEMENT_HTML.index('id="lessons-panel"'),
            MANAGEMENT_HTML.index('id="settings-panel"'),
        )
        self.assertNotIn("<h2>Other calendar feeds</h2>", MANAGEMENT_HTML)
        self.assertNotIn('id="ical"', MANAGEMENT_HTML)
        self.assertNotIn('id="feeds"', MANAGEMENT_HTML)

    def test_tutor_intro_is_a_separate_landing_page(self) -> None:
        self.assertIn("digital duct tape", TUTOR_LANDING_HTML)
        self.assertIn("dramatic puff of experimental software", TUTOR_LANDING_HTML)
        self.assertNotIn(
            "The app stores only the account, scheduling, booking, and payment-tracking",
            TUTOR_LANDING_HTML,
        )
        self.assertIn("Connect your calendars", TUTOR_LANDING_HTML)
        self.assertIn('/api/scheduling/assets/coffee-qr.png', TUTOR_LANDING_HTML)
        self.assertNotIn("digital duct tape", MANAGEMENT_HTML)
        self.assertNotIn('id="welcome-panel"', MANAGEMENT_HTML)

    def test_dashboard_header_links_to_separate_feedback_page(self) -> None:
        self.assertIn('data-slug="okserm"', MANAGEMENT_HTML)
        self.assertIn('class="dashboard-support"', MANAGEMENT_HTML)
        self.assertNotIn('class="dashboard-footer"', MANAGEMENT_HTML)
        self.assertIn('href="/schedule/feedback"', MANAGEMENT_HTML)
        self.assertIn('.dashboard-support .bug-link', MANAGEMENT_HTML)
        self.assertNotIn('.bug-link{display:inline-flex', MANAGEMENT_HTML)
        self.assertNotIn('id="bug-report"', MANAGEMENT_HTML)
        self.assertIn('id="bug-report"', BUG_REPORT_HTML)
        self.assertIn('data-action="bug-report"', BUG_REPORT_HTML)
        self.assertIn("Please do not include passwords", BUG_REPORT_HTML)
        self.assertNotIn("buymeacoffee", PUBLIC_HTML.casefold())
        self.assertNotIn("bug-report", PUBLIC_HTML)

    async def test_feedback_page_requires_tutor_and_shows_account_control(self) -> None:
        signed_out = await scheduling_feedback_page(_request())
        self.assertEqual(signed_out.status_code, 303)
        self.assertEqual(
            signed_out.headers["location"], "/auth/google/start?next=scheduling"
        )

        with patch("app.routes.scheduling.get_settings", return_value=self.settings):
            signed_in = await scheduling_feedback_page(
                _request(session={"google_email": "tutor@example.com"})
            )
        body = signed_in.body.decode()
        self.assertIn("Report a bug", body)
        self.assertIn("Signed in as", body)
        self.assertIn("tutor@example.com", body)
        self.assertIn('href="/schedule/manage"', body)

    def test_tutor_actions_use_an_in_page_confirmation_modal(self) -> None:
        self.assertIn('id="confirm-dialog"', MANAGEMENT_HTML)
        self.assertIn("requestConfirmation('Cancel this lesson?'", MANAGEMENT_HTML)
        self.assertIn("requestConfirmation('Disconnect iCloud?'", MANAGEMENT_HTML)
        self.assertIn("requestConfirmation('Remove this student?'", MANAGEMENT_HTML)
        self.assertIn("requestConfirmation('Delete this payment?'", MANAGEMENT_HTML)
        self.assertNotIn("confirm(", MANAGEMENT_HTML)
        self.assertNotIn("alert(", MANAGEMENT_HTML)
        self.assertNotIn("prompt(", MANAGEMENT_HTML)

    def test_public_booking_uses_calendar_layout_and_has_no_eyebrow(self) -> None:
        self.assertIn("width:min(1280px,100%)", PUBLIC_HTML)
        self.assertIn("width:min(1100px,100%)", PUBLIC_HTML)
        self.assertIn('grid-template-columns:260px minmax(0,1fr)', PUBLIC_HTML)
        self.assertIn(
            'grid-template-columns:repeat(7,minmax(36px,52px))', PUBLIC_HTML
        )
        self.assertIn('justify-content:space-between;gap:8px 6px', PUBLIC_HTML)
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

    async def test_public_booking_places_info_and_selected_times_below_calendar(self) -> None:
        response = await public_booking_page(_request(), "oksana-erm")
        body = response.body.decode()

        calendar_position = body.index('class="calendar-layout"')
        selected_position = body.index('id="selected-summary"')
        pricing_position = body.index("<h3>Pricing</h3>")
        self.assertLess(calendar_position, selected_position)
        self.assertLess(selected_position, pricing_position)
        self.assertIn("manage cancellations and rescheduling", body)
        self.assertIn("Book one session as a guest", body)
        self.assertIn('id="selected-list"', SELECTED_SUMMARY_HTML)
        self.assertIn("Booking conditions", PUBLIC_INFO_HTML)

    async def test_signed_in_student_sees_account_and_sign_out(self) -> None:
        response = await public_booking_page(
            _request(session={"student_google_email": "student@example.com"}),
            "oksana-erm",
        )
        body = response.body.decode()

        self.assertIn("Signed in as", body)
        self.assertIn("student@example.com", body)
        self.assertIn("/auth/student/logout?slug=oksana-erm", body)
        self.assertLess(body.index('id="account"'), body.index('<main class="shell">'))
        page_header = body[body.index('<header class="page-header">') : body.index("</header>")]
        self.assertNotIn('id="account"', page_header)
        self.assertIn(".page-account{position:fixed;top:20px;right:24px", body)

    async def test_signed_out_booking_uses_same_top_right_account_position(self) -> None:
        response = await public_booking_page(_request(), "oksana-erm")
        body = response.body.decode()

        self.assertIn('<div class="page-account" id="account">', body)
        self.assertIn('class="account-sign-in"', body)
        self.assertIn(">Sign in</a>", body)
        self.assertIn(ACCOUNT_CONTROL_CSS, body)

    async def test_student_logout_preserves_tutor_session(self) -> None:
        session = {
            "student_google_email": "student@example.com",
            "student_google_name": "Student",
            "google_email": "tutor@example.com",
        }
        response = await student_logout(_request(session=session), slug="oksana-erm")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/book/oksana-erm")
        self.assertNotIn("student_google_email", session)
        self.assertNotIn("student_google_name", session)
        self.assertEqual(session["google_email"], "tutor@example.com")

        unsafe_response = await student_logout(_request(session={}), slug="//attacker.example")
        self.assertEqual(unsafe_response.headers["location"], "/")

    def test_student_lesson_changes_use_an_in_page_modal(self) -> None:
        self.assertIn('id="lesson-modal"', STUDENT_HTML)
        self.assertIn("openLessonModal(x.id,false)", STUDENT_HTML)
        self.assertIn("openLessonModal(x.id,true)", STUDENT_HTML)
        self.assertIn("Lesson cancelled and the credit was restored.", STUDENT_HTML)
        self.assertNotIn("confirm(", STUDENT_HTML)
        self.assertNotIn("alert(", STUDENT_HTML)

    def test_past_student_lessons_are_dimmed_and_not_joinable(self) -> None:
        self.assertIn(".lesson.past:not(.cancelled){opacity:.42", STUDENT_HTML)
        self.assertIn("isPast=x.is_past||new Date(x.ends_at)<=new Date()", STUDENT_HTML)
        self.assertIn("isPast?' past'", STUDENT_HTML)
        self.assertIn("isPast?'Past'", STUDENT_HTML)
        self.assertIn("!isPast&&x.meeting_url", STUDENT_HTML)

    async def test_student_account_matches_booking_account_control(self) -> None:
        response = await student_lessons_page(
            _request(session={"student_google_email": "student@example.com"}),
            "oksana-erm",
        )
        body = response.body.decode()

        self.assertIn(ACCOUNT_CONTROL_CSS, STUDENT_HTML)
        self.assertIn('<div class="page-account" id="account">', body)
        self.assertIn('<div class="signed-in">', body)
        self.assertIn('id="account-email">student@example.com', body)
        self.assertNotIn('<div class="account">', body)

    def test_cancelled_lessons_are_distinct_and_sorted_after_active_lessons(self) -> None:
        self.assertIn(".lesson.cancelled{opacity:.62", STUDENT_HTML)
        self.assertIn("border-style:dashed", STUDENT_HTML)
        self.assertIn("x.status==='cancelled'?' cancelled':''", STUDENT_HTML)
        self.assertIn(
            "Number(a.status==='cancelled')-Number(b.status==='cancelled')",
            STUDENT_HTML,
        )
        self.assertIn("lessonOrder(d.lessons).map(lessonNode)", STUDENT_HTML)

    def test_public_confirmation_has_structured_lesson_summary(self) -> None:
        self.assertIn('class="success-sketch"', PUBLIC_HTML)
        self.assertIn('id="success-time"', PUBLIC_HTML)
        self.assertIn('id="success-meta"', PUBLIC_HTML)
        self.assertIn("Your lesson is booked", PUBLIC_HTML)
        self.assertIn("Your permanent Google Meet link", PUBLIC_HTML)
        self.assertNotIn("$('#success').textContent=", PUBLIC_HTML)

    def test_public_booking_offers_guest_session_and_google_benefits(self) -> None:
        self.assertIn("Continue with Google", PUBLIC_HTML)
        self.assertIn("Book one session without signing in", PUBLIC_HTML)
        self.assertIn('class="signin-sketch"', PUBLIC_HTML)
        self.assertIn(
            "Sign in with Google to book multiple lessons, manage cancellations and "
            "rescheduling, track lesson credits, and reuse your permanent Meet link.",
            PUBLIC_HTML,
        )
        self.assertIn("shell.signin-mode", PUBLIC_HTML)
        self.assertIn("Choose up to 10 times", PUBLIC_HTML)
        self.assertIn("guestMode?1:10", PUBLIC_HTML)
        self.assertIn('id="guest-details"', PUBLIC_HTML)
        self.assertIn('name="student_name"', PUBLIC_HTML)
        self.assertIn('name="student_email"', PUBLIC_HTML)
        self.assertIn("Your one-time Google Meet link", PUBLIC_HTML)
        self.assertIn("starts_at:selectedStarts.map", PUBLIC_HTML)

    async def test_authenticated_lessons_root_opens_management(self) -> None:
        with patch("app.main.get_settings", return_value=self.settings):
            response = await root(_request(session={"google_email": "tutor@example.com"}))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/schedule/manage")

    async def test_signed_out_lessons_root_opens_tutor_landing(self) -> None:
        with patch("app.main.get_settings", return_value=self.settings):
            response = await root(_request())

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/schedule")

    async def test_tutor_landing_and_dashboard_show_account_controls(self) -> None:
        signed_out = await tutor_landing_page(_request())
        signed_out_body = signed_out.body.decode()
        self.assertIn('id="tutor-account"', signed_out_body)
        self.assertIn("Sign in</a>", signed_out_body)
        self.assertIn("Create your free tutor page with Google", signed_out_body)

        signed_in = await tutor_landing_page(
            _request(session={"google_email": "tutor@example.com"})
        )
        self.assertIn("Signed in as", signed_in.body.decode())
        self.assertIn("Open tutor dashboard", signed_in.body.decode())

        dashboard = await scheduling_management_page(
            _request(session={"google_email": "tutor@example.com"})
        )
        dashboard_body = dashboard.body.decode()
        self.assertIn('id="tutor-account"', dashboard_body)
        self.assertIn("tutor@example.com", dashboard_body)
        self.assertIn('/auth/logout?next=scheduling', dashboard_body)

    async def test_tutor_logout_returns_to_landing(self) -> None:
        request = _request(session={"google_email": "tutor@example.com"})
        response = await logout(request, next="scheduling")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/schedule")
        self.assertEqual(request.session, {})

        family_response = await logout(
            _request(session={"google_email": "family@example.com"})
        )
        self.assertEqual(family_response.headers["location"], "/dashboard")

    async def test_management_page_has_no_family_dashboard_link(self) -> None:
        response = await scheduling_management_page(
            _request(session={"google_email": "tutor@example.com"})
        )

        body = response.body.decode()
        self.assertNotIn("Family Copilot", body)
        self.assertNotIn(">Dashboard<", body)

    def test_lessons_host_exposes_only_scheduling_and_required_login_routes(self) -> None:
        self.assertTrue(scheduling_host_allows_path("/schedule"))
        self.assertTrue(scheduling_host_allows_path("/schedule/feedback"))
        self.assertTrue(scheduling_host_allows_path("/schedule/manage"))
        self.assertTrue(scheduling_host_allows_path("/schedule/register"))
        self.assertTrue(scheduling_host_allows_path("/schedule/admin"))
        self.assertTrue(scheduling_host_allows_path("/book/oksana-erm"))
        self.assertTrue(scheduling_host_allows_path("/api/scheduling/manage"))
        self.assertTrue(scheduling_host_allows_path("/calendar/google/start"))
        self.assertTrue(scheduling_host_allows_path("/auth/student/logout"))
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

        with patch("app.routes.auth.get_settings", return_value=self.settings):
            await google_auth_start(request, next="book:oksana-erm")

        self.assertEqual(request.session["oauth_next"], "book:oksana-erm")

    def test_registration_collects_country_subjects_and_browser_timezone(self) -> None:
        self.assertIn('name="country"', REGISTRATION_HTML)
        self.assertIn('name="tutoring_subjects"', REGISTRATION_HTML)
        self.assertIn('name="timezone"', REGISTRATION_HTML)
        self.assertIn("Intl.DateTimeFormat().resolvedOptions().timeZone", REGISTRATION_HTML)

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
