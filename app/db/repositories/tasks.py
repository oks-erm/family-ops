from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task, TaskCompletion, TaskStatus


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_task(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        title: str,
        due_date: date | None,
        category: str | None = None,
    ) -> Task:
        task = Task(
            user_id=user_id,
            household_id=household_id,
            title=title,
            category=category,
            due_date=due_date,
            status=TaskStatus.pending,
        )
        self.session.add(task)
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def list_pending_for_household(
        self,
        *,
        household_id: UUID,
        through_date: date | None = None,
    ) -> list[Task]:
        query = select(Task).where(
            Task.household_id == household_id,
            Task.status == TaskStatus.pending,
        )
        if through_date is not None:
            query = query.where((Task.due_date.is_(None)) | (Task.due_date <= through_date))
        result = await self.session.execute(query.order_by(Task.due_date.nulls_last(), Task.created_at))
        return list(result.scalars().all())

    async def list_pending_for_user(
        self,
        *,
        user_id: UUID,
        through_date: date | None = None,
    ) -> list[Task]:
        query = select(Task).where(
            Task.user_id == user_id,
            Task.status == TaskStatus.pending,
        )
        if through_date is not None:
            query = query.where((Task.due_date.is_(None)) | (Task.due_date <= through_date))
        result = await self.session.execute(query.order_by(Task.due_date.nulls_last(), Task.created_at))
        return list(result.scalars().all())

    async def get_household_task(self, *, task_id: UUID, household_id: UUID) -> Task | None:
        result = await self.session.execute(
            select(Task).where(Task.id == task_id, Task.household_id == household_id)
        )
        return result.scalar_one_or_none()

    async def get_user_task(self, *, task_id: UUID, user_id: UUID) -> Task | None:
        result = await self.session.execute(select(Task).where(Task.id == task_id, Task.user_id == user_id))
        return result.scalar_one_or_none()

    async def find_pending_by_title(self, *, user_id: UUID, title: str) -> Task | None:
        normalized = title.strip().lower()
        canonical = self._canonical_title(title)
        result = await self.session.execute(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == TaskStatus.pending,
            )
            .order_by(Task.due_date.nulls_last(), Task.created_at.desc())
        )
        for task in result.scalars().all():
            task_title = task.title.strip().lower()
            task_canonical = self._canonical_title(task.title)
            if (
                task_title == normalized
                or normalized in task_title
                or task_title in normalized
                or task_canonical == canonical
                or canonical in task_canonical
                or task_canonical in canonical
            ):
                return task
        return None

    @staticmethod
    def _canonical_title(title: str) -> str:
        words = []
        for raw_word in title.strip().lower().split():
            word = raw_word.strip(" .")
            if word in {"the", "a", "an", "to"}:
                continue
            if word.endswith("ing") and len(word) > 5:
                word = word[:-3]
                if word.endswith("is"):
                    word += "e"
            words.append(word)
        return " ".join(words)

    async def list_completed_for_user_on_date(self, *, user_id: UUID, day: date) -> list[Task]:
        result = await self.session.execute(
            select(Task)
            .where(
                Task.user_id == user_id,
                Task.status == TaskStatus.done,
                Task.due_date == day,
            )
            .order_by(Task.updated_at.desc())
        )
        return list(result.scalars().all())

    async def mark_pending_by_title_done(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        title: str,
        completed_on: date,
    ) -> Task | None:
        task = await self.find_pending_by_title(user_id=user_id, title=title)
        if task is None:
            return None
        task.status = TaskStatus.done
        task.due_date = task.due_date or completed_on
        self.session.add(
            TaskCompletion(
                task_id=task.id,
                user_id=user_id,
                household_id=household_id,
                completed_on=completed_on,
                status=TaskStatus.done,
            )
        )
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def create_completed_task(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        title: str,
        completed_on: date,
        category: str | None = None,
    ) -> Task:
        task = Task(
            user_id=user_id,
            household_id=household_id,
            title=title,
            category=category,
            due_date=completed_on,
            status=TaskStatus.done,
        )
        self.session.add(task)
        await self.session.flush()
        self.session.add(
            TaskCompletion(
                task_id=task.id,
                user_id=user_id,
                household_id=household_id,
                completed_on=completed_on,
                status=TaskStatus.done,
            )
        )
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def remove_pending_by_title(self, *, user_id: UUID, title: str) -> Task | None:
        task = await self.find_pending_by_title(user_id=user_id, title=title)
        if task is None:
            return None
        task.status = TaskStatus.skipped
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def move_pending_by_title(
        self,
        *,
        user_id: UUID,
        title: str,
        due_date: date,
    ) -> Task | None:
        task = await self.find_pending_by_title(user_id=user_id, title=title)
        if task is None:
            return None
        task.due_date = due_date
        task.moved_count += 1
        await self.session.commit()
        await self.session.refresh(task)
        return task

    async def apply_action_by_title(
        self,
        *,
        user_id: UUID,
        household_id: UUID,
        title: str,
        action: str,
        today: date,
    ) -> Task | None:
        task = await self.find_pending_by_title(user_id=user_id, title=title)
        if task is None:
            return None
        return await self.apply_action(
            task_id=task.id,
            user_id=user_id,
            household_id=household_id,
            action=action,
            today=today,
        )

    async def apply_action(
        self,
        *,
        task_id: UUID,
        user_id: UUID,
        household_id: UUID,
        action: str,
        today: date,
    ) -> Task | None:
        task = await self.get_user_task(task_id=task_id, user_id=user_id)
        if task is None:
            return None

        if action == "done":
            task.status = TaskStatus.done
            completion_status = TaskStatus.done
        elif action == "skip":
            task.status = TaskStatus.skipped
            completion_status = TaskStatus.skipped
        elif action == "move":
            task.status = TaskStatus.pending
            task.due_date = today + timedelta(days=1)
            task.moved_count += 1
            completion_status = TaskStatus.moved
        else:
            return None

        self.session.add(
            TaskCompletion(
                task_id=task.id,
                user_id=user_id,
                household_id=household_id,
                completed_on=today,
                status=completion_status,
            )
        )
        await self.session.commit()
        await self.session.refresh(task)
        return task
