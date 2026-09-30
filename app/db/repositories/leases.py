"""Renewable, fenced leases; no connection is held while waiting for a model."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import RuntimeLease


class LeaseBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class LeaseRepository:
    def __init__(self, engine):
        self.factory = async_sessionmaker(engine, expire_on_commit=False)

    async def acquire(self, name, owner, seconds):
        async with self.factory() as session:
            statement = insert(RuntimeLease).values(
                name=name,
                owner=owner,
                expires_at=func.clock_timestamp() + timedelta(seconds=seconds),
            )
            result = await session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[RuntimeLease.name],
                    set_={"owner": owner, "expires_at": statement.excluded.expires_at},
                    where=RuntimeLease.expires_at < func.clock_timestamp(),
                ).returning(RuntimeLease.owner)
            )
            await session.commit()
            return result == owner

    async def renew(self, name, owner, seconds):
        async with self.factory() as session:
            result = await session.scalar(
                update(RuntimeLease)
                .where(
                    RuntimeLease.name == name,
                    RuntimeLease.owner == owner,
                    RuntimeLease.expires_at > func.clock_timestamp(),
                )
                .values(expires_at=func.clock_timestamp() + timedelta(seconds=seconds))
                .returning(RuntimeLease.owner)
            )
            await session.commit()
            return result == owner

    async def release(self, name, owner):
        async with self.factory() as session:
            await session.execute(
                update(RuntimeLease)
                .where(
                    RuntimeLease.name == name,
                    RuntimeLease.owner == owner,
                )
                .values(expires_at=func.clock_timestamp())
            )
            await session.commit()

    @staticmethod
    async def fence(session, name, owner):
        # This row lock lasts only through the caller's database transaction/commit.
        lease = await session.scalar(
            select(RuntimeLease)
            .where(
                RuntimeLease.name == name,
                RuntimeLease.owner == owner,
                RuntimeLease.expires_at > func.clock_timestamp(),
            )
            .with_for_update()
        )
        if lease is None:
            raise LeaseLost("The worker no longer owns this conversation")

    @asynccontextmanager
    async def held(self, name, seconds=60):
        owner = uuid4()
        if not await self.acquire(name, owner, seconds):
            raise LeaseBusy(name)
        parent = asyncio.current_task()
        lost = False

        async def heartbeat():
            nonlocal lost
            try:
                while True:
                    await asyncio.sleep(seconds / 4)
                    async with asyncio.timeout(seconds / 3):
                        if not await self.renew(name, owner, seconds):
                            raise LeaseLost(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                lost = True
                parent.cancel()

        task = asyncio.create_task(heartbeat())
        try:
            yield owner
        except asyncio.CancelledError:
            if lost:
                raise LeaseLost(name) from None
            raise
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            # A lost owner cannot release the replacement worker's lease.
            await self.release(name, owner)
