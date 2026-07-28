import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.db.models import SchedulingProfile, StudentPayment, User
from app.routes.auth import google_auth_callback
from app.routes.dashboard import _dashboard_context as family_dashboard_context
from app.routes.scheduling import (
    MANAGEMENT_HTML,
    _require_superadmin,
    scheduling_admin_page,
    scheduling_admin_stats,
    tutor_registration_page,
)


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


class TutorRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_google_tutor_is_sent_to_registration(self) -> None:
        request = _request(
            session={"oauth_state": "state-123", "oauth_next": "scheduling"}
        )
        settings = Settings(
            _env_file=None,
            GOOGLE_CLIENT_ID="client",
            GOOGLE_CLIENT_SECRET="secret",
            PUBLIC_BASE_URL="https://example.com",
            SCHEDULING_PUBLIC_BASE_URL="https://lessons.example.com",
        )
        token_response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"access_token": "token"},
        )
        user_response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"email": "New@Example.com", "name": "New Tutor"},
        )
        client = AsyncMock()
        client.post.return_value = token_response
        client.get.return_value = user_response
        client_context = AsyncMock()
        client_context.__aenter__.return_value = client
        client_context.__aexit__.return_value = False

        @asynccontextmanager
        async def session_factory():
            yield AsyncMock()

        with (
            patch("app.routes.auth.get_settings", return_value=settings),
            patch("app.routes.auth.httpx.AsyncClient", return_value=client_context),
            patch("app.routes.auth.async_session_factory", session_factory),
            patch(
                "app.routes.auth.UserRepository.get_by_google_email",
                new=AsyncMock(return_value=None),
            ),
        ):
            response = await google_auth_callback(
                request,
                code="oauth-code",
                state="state-123",
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "https://lessons.example.com/schedule/register",
        )
        self.assertEqual(request.session["pending_tutor_email"], "new@example.com")
        self.assertEqual(request.session["pending_tutor_name"], "New Tutor")

    async def test_registration_requires_verified_pending_google_identity(self) -> None:
        response = await tutor_registration_page(_request())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/google/start?next=scheduling")

        response = await tutor_registration_page(
            _request(session={"pending_tutor_email": "new@example.com"})
        )
        self.assertEqual(response.status_code, 200)
        body = response.body.decode()
        self.assertIn("Create my tutor page", body)
        self.assertIn("/api/scheduling/register", body)

    async def test_scheduling_only_user_is_rejected_by_family_dashboard(self) -> None:
        request = _request(session={"google_email": "tutor@example.com"})
        user = SimpleNamespace(family_dashboard_enabled=False)
        session = AsyncMock()
        with patch(
            "app.routes.dashboard.UserRepository.get_by_google_email",
            new=AsyncMock(return_value=user),
        ):
            result = await family_dashboard_context(request, session)

        self.assertEqual(result, (None, None))
        self.assertEqual(request.session, {})

    def test_model_defaults_preserve_existing_eur_pricing(self) -> None:
        self.assertTrue(User.__table__.c.telegram_user_id.nullable)
        self.assertTrue(User.__table__.c.telegram_chat_id.nullable)
        self.assertTrue(User.__table__.c.family_dashboard_enabled.default.arg)
        self.assertEqual(SchedulingProfile.__table__.c.currency.default.arg, "EUR")
        self.assertEqual(SchedulingProfile.__table__.c.hourly_rate_cents.default.arg, 3000)
        self.assertEqual(
            SchedulingProfile.__table__.c.cancellation_notice_hours.default.arg, 12
        )
        self.assertEqual(StudentPayment.__table__.c.currency.default.arg, "EUR")

    def test_management_has_editable_pricing_packages_and_policy(self) -> None:
        self.assertIn('id="pricing"', MANAGEMENT_HTML)
        self.assertIn('name="currency"', MANAGEMENT_HTML)
        self.assertIn('name="hourly_rate"', MANAGEMENT_HTML)
        self.assertIn('id="packages"', MANAGEMENT_HTML)
        self.assertIn("Package discount percent", MANAGEMENT_HTML)
        self.assertIn('name="cancellation_notice_hours"', MANAGEMENT_HTML)
        self.assertIn('name="cancellation_policy_text"', MANAGEMENT_HTML)
        self.assertIn("/api/scheduling/pricing", MANAGEMENT_HTML)
        self.assertIn("/api/scheduling/packages", MANAGEMENT_HTML)
        self.assertIn("currency=state.profile.currency", MANAGEMENT_HTML)


class SchedulingAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            SCHEDULING_FEEDBACK_TO_EMAIL="owner@example.com",
        )

    def test_feedback_recipient_is_initial_superadmin(self) -> None:
        with patch("app.routes.scheduling.get_settings", return_value=self.settings):
            self.assertEqual(
                _require_superadmin(
                    _request(session={"google_email": "OWNER@example.com"})
                ),
                "owner@example.com",
            )
            with self.assertRaises(HTTPException) as raised:
                _require_superadmin(
                    _request(session={"google_email": "tutor@example.com"})
                )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_admin_page_contains_aggregates_without_searchable_list(self) -> None:
        with patch("app.routes.scheduling.get_settings", return_value=self.settings):
            response = await scheduling_admin_page(
                _request(session={"google_email": "owner@example.com"})
            )
        body = response.body.decode()
        self.assertIn("Registered tutors", body)
        self.assertIn("Active booking pages", body)
        self.assertIn("New this month", body)
        self.assertNotIn("Search", body)

    async def test_admin_statistics_are_aggregate_only(self) -> None:
        profiles = [
            SimpleNamespace(
                is_active=True,
                country="Portugal",
                tutoring_subjects="English",
                created_at=datetime.now(UTC),
            ),
            SimpleNamespace(
                is_active=False,
                country="Portugal",
                tutoring_subjects="Maths",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        ]
        session = AsyncMock()
        session.execute.return_value = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: profiles)
        )

        @asynccontextmanager
        async def session_factory():
            yield session

        with (
            patch("app.routes.scheduling.get_settings", return_value=self.settings),
            patch("app.routes.scheduling.async_session_factory", session_factory),
        ):
            result = await scheduling_admin_stats(
                _request(session={"google_email": "owner@example.com"})
            )

        self.assertEqual(result["total_tutors"], 2)
        self.assertEqual(result["active_booking_pages"], 1)
        self.assertEqual(result["new_this_month"], 1)
        self.assertEqual(result["countries"], [{"label": "Portugal", "count": 2}])
        self.assertNotIn("tutors", result)


if __name__ == "__main__":
    unittest.main()
