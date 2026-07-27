import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from starlette.requests import Request

from app.db.models import HiddenSchedulingStudent
from app.routes.scheduling import delete_student_payment, remove_scheduling_student


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": [
                (b"host", b"lessons.example.com"),
                (b"origin", b"https://lessons.example.com"),
            ],
            "server": ("lessons.example.com", 443),
            "session": {"google_email": "tutor@example.com"},
        }
    )


class SchedulingManagementDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleting_registered_payment_removes_its_allocations_via_cascade(self) -> None:
        profile = SimpleNamespace(id=uuid4())
        payment = SimpleNamespace(id=uuid4(), profile_id=profile.id)
        session = AsyncMock()
        session.get.return_value = payment

        @asynccontextmanager
        async def session_factory():
            yield session

        with (
            patch("app.routes.scheduling.async_session_factory", session_factory),
            patch(
                "app.routes.scheduling._management_profile",
                new=AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(), profile)),
            ),
        ):
            result = await delete_student_payment(_request(), payment.id)

        self.assertEqual(result, {"deleted": True})
        session.delete.assert_awaited_once_with(payment)
        session.commit.assert_awaited_once()

    async def test_student_with_history_is_hidden_without_deleting_records(self) -> None:
        profile = SimpleNamespace(id=uuid4())
        session = AsyncMock()
        session.add = Mock()
        session.scalar.side_effect = [1, 0, None]

        @asynccontextmanager
        async def session_factory():
            yield session

        with (
            patch("app.routes.scheduling.async_session_factory", session_factory),
            patch(
                "app.routes.scheduling._management_profile",
                new=AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(), profile)),
            ),
        ):
            result = await remove_scheduling_student(
                _request(), " Student@Example.com "
            )

        self.assertEqual(result, {"deleted": False, "hidden": True})
        hidden = session.add.call_args.args[0]
        self.assertIsInstance(hidden, HiddenSchedulingStudent)
        self.assertEqual(hidden.profile_id, profile.id)
        self.assertEqual(hidden.student_email, "student@example.com")
        session.execute.assert_not_awaited()
        session.commit.assert_awaited_once()

    async def test_student_without_history_is_deleted_completely(self) -> None:
        profile = SimpleNamespace(id=uuid4())
        session = AsyncMock()
        session.scalar.side_effect = [0, 0]

        @asynccontextmanager
        async def session_factory():
            yield session

        with (
            patch("app.routes.scheduling.async_session_factory", session_factory),
            patch(
                "app.routes.scheduling._management_profile",
                new=AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(), profile)),
            ),
        ):
            result = await remove_scheduling_student(
                _request(), "student@example.com"
            )

        self.assertEqual(result, {"deleted": True, "hidden": False})
        self.assertEqual(session.execute.await_count, 2)
        session.add.assert_not_called()
        session.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
