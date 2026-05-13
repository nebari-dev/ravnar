from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from math import ceil
from typing import Any, cast

from fastapi import HTTPException, status
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import Engine, Select, asc, create_engine, desc, func, inspect, literal_column, select
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.interfaces import ORMOption
from starlette.concurrency import run_in_threadpool
from typing_extensions import TypedDict

from . import orm, schema
from .mixin import SetupTeardownMixin
from .utils import as_async_context_manager, as_awaitable, now


class SessionFactoryParams(TypedDict):
    expire_on_commit: bool


class Database(SetupTeardownMixin):
    def __init__(self, url: str) -> None:
        url = make_url(url)

        if url.drivername.startswith("sqlite") and (url.database is None or url.database == ":memory:"):
            # See https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#using-a-memory-database-in-multiple-threads
            q = dict(url.query)
            q.setdefault("check_same_thread", "false")
            url = url.update_query_dict(q)  # type: ignore[arg-type]

        engine: Engine | AsyncEngine
        try:
            engine = create_async_engine(url)
        except InvalidRequestError:
            engine = create_engine(url)
        self._engine = engine
        self._session_factory: (
            Callable[[], AbstractContextManager[Session]] | Callable[[], AbstractAsyncContextManager[AsyncSession]]
        )

    async def setup(self) -> None:  # type: ignore[override]
        session_factory_params = SessionFactoryParams(expire_on_commit=False)

        if isinstance(self._engine, Engine):
            SQLAlchemyInstrumentor().instrument(
                engine=self._engine,
            )

            orm.Base.metadata.create_all(bind=self._engine)
            self._session_factory = sessionmaker(bind=self._engine, **session_factory_params)

        else:
            SQLAlchemyInstrumentor().instrument(
                engine=self._engine.sync_engine,
            )

            async with self._engine.begin() as conn:
                await conn.run_sync(orm.Base.metadata.create_all)

            self._session_factory = async_sessionmaker(bind=self._engine, **session_factory_params)

    async def teardown(self) -> None:  # type: ignore[override]
        await as_awaitable(self._engine.dispose)

    @contextlib.asynccontextmanager
    async def _get_session(self) -> AsyncIterator[AsyncSession]:
        async with as_async_context_manager(self._session_factory()) as session:
            session = cast(Session | AsyncSession, session)
            async with as_async_context_manager(session.begin()):
                if isinstance(session, Session):
                    # Instead of using as_awaitable everywhere, the SyncSessionWrapper allows us to pretend we have an
                    # AsyncSession and thus simplify typing and reduce boilerplate
                    session = cast(AsyncSession, SyncSessionWrapper(session))

                yield session

    async def _get_page(
        self,
        session: AsyncSession,
        *,
        orm_type: type[orm.TOrm],
        select_qualifier: Callable[[Select], Select] = lambda query: query,
        load_options: Collection[ORMOption] | None = None,
        pagination: schema.Pagination | None = None,
    ) -> orm.Page[orm.TOrm]:
        if pagination is None:
            pagination = schema.Pagination.as_single_page()

        query = select_qualifier(select(orm_type))

        result = await session.execute(select(func.count()).select_from(query.subquery()))
        total_count = result.scalar_one()
        page_size = total_count if pagination.is_single_page else min(total_count, pagination.page_size)
        page_count = ceil(total_count / page_size) if page_size > 0 else 1

        if total_count > 0:
            load_query = query.options(*load_options) if load_options is not None else query

            if pagination.sort_by is not None:
                sort_attr = getattr(orm_type, pagination.sort_by)
                order_fn = asc if pagination.sort_order == "ascending" else desc
                load_query = load_query.order_by(order_fn(sort_attr))

            # always append the primary key to get a stable sort
            load_query = load_query.order_by(*inspect(orm_type).primary_key)

            if not pagination.is_single_page:
                load_query = load_query.limit(pagination.page_size)

                if pagination.page_number is not None:
                    offset = (pagination.page_number - 1) * pagination.page_size
                    load_query = load_query.offset(offset)

            result = await session.execute(load_query)
            items = result.unique().scalars().all()
        else:
            items = []

        return orm.Page(
            page_size=page_size,
            page_number=pagination.page_number,
            total_count=total_count,
            page_count=page_count,
            items=items,
        )

    async def add_file(self, file: orm.File) -> None:
        async with self._get_session() as session:
            session.add(file)

    async def _get_file(self, session: AsyncSession, *, id: uuid.UUID, user_id: str) -> orm.File:
        result = await session.execute(select(orm.File).where((orm.File.id == id) & (orm.File.user_id == user_id)))
        file = result.scalar_one_or_none()
        if file is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
        return file

    async def get_file(self, *, id: uuid.UUID, user_id: str) -> orm.File:
        async with self._get_session() as session:
            return await self._get_file(session, id=id, user_id=user_id)

    async def delete_file(self, *, id: uuid.UUID, user_id: str) -> None:
        async with self._get_session() as session:
            file = await self._get_file(session, id=id, user_id=user_id)
            await session.delete(file)

    async def create_thread(self, *, user_id: str, id: str, name: str | None, agent_id: str) -> orm.Thread:
        async with self._get_session() as session:
            query = select(orm.Thread).where(orm.Thread.id == id)
            result = await session.execute(query)
            thread = result.scalar_one_or_none()
            if thread is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Thread exists")

            thread = orm.Thread(
                id=id,
                user_id=user_id,
                agent_id=agent_id,
                name=name,
                created_at=now(),
                runs=[],
            )
            session.add(thread)
            return thread

    async def _get_threads(
        self,
        session: AsyncSession,
        user_id: str,
        ids: list[str] | None = None,
        pagination: schema.Pagination | None = None,
    ) -> orm.Page[orm.Thread]:
        def select_qualifier(query: Select) -> Select:
            query = query.where(orm.Thread.user_id == user_id)
            if ids is not None:
                query = query.where(orm.Thread.id.in_(ids))
            return query

        return await self._get_page(
            session, orm_type=orm.Thread, select_qualifier=select_qualifier, pagination=pagination
        )

    async def get_threads(self, *, user_id: str, pagination: schema.Pagination) -> orm.Page[orm.Thread]:
        async with self._get_session() as session:
            return await self._get_threads(session, user_id=user_id, pagination=pagination)

    async def _get_thread(self, session: AsyncSession, *, user_id: str, id: str) -> orm.Thread:
        query = select(orm.Thread).where((orm.Thread.id == id) & (orm.Thread.user_id == user_id))
        result = await session.execute(query)
        thread = result.scalar_one_or_none()
        if thread is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
        return thread

    async def get_thread(self, *, user_id: str, id: str) -> orm.Thread:
        async with self._get_session() as session:
            return await self._get_thread(session, user_id=user_id, id=id)

    async def rename_thread(self, *, user_id: str, id: str, name: str) -> orm.Thread:
        async with self._get_session() as session:
            thread = await self._get_thread(session, user_id=user_id, id=id)
            thread.name = name
            return thread

    async def create_run(self, *, run: orm.Run) -> None:
        async with self._get_session() as session:
            session.add(run)

    async def _get_run(self, session: AsyncSession, *, id: str, user_id: str) -> orm.Run:
        query = (
            select(orm.Run)
            .join(orm.Thread, orm.Run.thread_id == orm.Thread.id)
            .where((orm.Run.id == id) & (orm.Thread.user_id == user_id))
        )
        result = await session.execute(query)
        run = result.scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        return run

    async def get_run(self, *, id: str, user_id: str) -> orm.Run:
        async with self._get_session() as session:
            return await self._get_run(session, id=id, user_id=user_id)

    async def get_runs(
        self,
        *,
        user_id: str,
        thread_id: str,
        pagination: schema.Pagination | None = None,
    ) -> orm.Page[orm.Run]:
        async with self._get_session() as session:

            def select_qualifier(query: Select) -> Select:
                return query.join(orm.Thread, orm.Run.thread_id == orm.Thread.id).where(
                    (orm.Run.thread_id == thread_id) & (orm.Thread.user_id == user_id)
                )

            return await self._get_page(
                session, orm_type=orm.Run, select_qualifier=select_qualifier, pagination=pagination
            )

    async def _get_thread_messages(self, session: AsyncSession, *, run_id: str) -> list[orm.Message]:
        run_chain = (
            select(
                orm.Run.id.label("run_id"),
                orm.Run.parent_run_id.label("parent_run_id"),
                literal_column("0").label("depth"),
            )
            .where(orm.Run.id == run_id)
            .cte(name="run_chain", recursive=True)
        )
        run_chain = run_chain.union_all(
            select(orm.Run.id, orm.Run.parent_run_id, (run_chain.c.depth + 1).label("depth"))
            .select_from(orm.Run)
            .join(run_chain, orm.Run.id == run_chain.c.parent_run_id)
        )

        ranked = (
            select(
                orm.Message.uid,
                func.row_number()
                .over(
                    partition_by=orm.Message.id,
                    order_by=run_chain.c.depth.asc(),
                )
                .label("rn"),
            )
            .join(run_chain, orm.Message.run_id == run_chain.c.run_id)
            .subquery()
        )

        query = (
            select(orm.Message)
            .join(ranked, orm.Message.uid == ranked.c.uid)
            .where(ranked.c.rn == 1)
            .order_by(orm.Message.created_at.asc(), orm.Message.id.asc())
        )

        result = await session.execute(query)
        return result.unique().scalars().all()

    async def get_thread_history(
        self, *, user_id: str, thread_id: str, run_id: str | None
    ) -> tuple[orm.Thread, orm.Run | None, list[orm.Message]]:
        async with self._get_session() as session:
            thread = await self._get_thread(session, user_id=user_id, id=thread_id)

            if run_id is None:
                if not thread.runs:
                    return thread, None, []
                run = thread.runs[-1]
            else:
                try:
                    run = next(r for r in thread.runs if r.id == run_id)
                except StopIteration:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent run not found") from None

            messages = await self._get_thread_messages(session, run_id=run.id)
            return thread, run, messages

    async def delete_threads(self, *, user_id: str, ids: list[str]) -> None:
        async with self._get_session() as session:
            single_page = await self._get_threads(session, user_id=user_id, ids=ids)
            threads = single_page.items
            if len(threads) != len(ids):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Threads not found")

            for t in threads:
                await session.delete(t)


class SyncSessionWrapper:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._attrs = {
            "delete",
            "execute",
            "merge",
        }

    def __getattr__(self, attr: str) -> Any:
        value = getattr(self._session, attr)
        if attr not in self._attrs:
            return value

        def wrapper(*args: Any, **kwargs: Any) -> Awaitable[Any]:
            return run_in_threadpool(value, *args, **kwargs)

        return wrapper
