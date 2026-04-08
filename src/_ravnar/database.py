from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from math import ceil
from typing import Any, cast

from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import Engine, Select, asc, create_engine, desc, func, inspect, select
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, selectinload, sessionmaker
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
        pagination: schema.Pagination,
    ) -> orm.Page[orm.TOrm]:
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

    async def create_thread(self, *, user_id: str, id: str, name: str | None, agent_id: str) -> orm.Thread:
        # FIXME: check if thread already exists
        async with self._get_session() as session:
            thread = orm.Thread(
                id=id, user_id=user_id, agent_id=agent_id, name=name, created_at=now(), state=None, messages=[]
            )
            session.add(thread)
            return thread

    async def _get_threads(
        self, session: AsyncSession, user_id: str, pagination: schema.Pagination
    ) -> orm.Page[orm.Thread]:
        return await self._get_page(
            session,
            orm_type=orm.Thread,
            select_qualifier=lambda query: query.where(orm.Thread.user_id == user_id),
            pagination=pagination,
        )

    async def get_threads(self, *, user_id: str, pagination: schema.Pagination) -> orm.Page[orm.Thread]:
        async with self._get_session() as session:
            return await self._get_threads(session, user_id=user_id, pagination=pagination)

    async def _get_thread(self, session: AsyncSession, *, user_id: str, id: str, with_messages: bool) -> orm.Thread:
        query = select(orm.Thread).where((orm.Thread.id == id) & (orm.Thread.user_id == user_id))
        if with_messages:
            query = query.options(selectinload(orm.Thread.messages))
        result = await session.execute(query)
        thread = result.scalar_one_or_none()
        if thread is None:
            raise Exception
        return thread

    async def get_thread(self, user_id: str, id: str, with_messages: bool = False) -> orm.Thread:
        async with self._get_session() as session:
            return await self._get_thread(session, user_id=user_id, id=id, with_messages=with_messages)

    async def append_messages_to_thread(self, *, user_id: str, id: str, messages: list[orm.Message]) -> orm.Thread:
        async with self._get_session() as session:
            query = select(orm.Thread).where((orm.Thread.id == id) & (orm.Thread.user_id == user_id))
            result = await session.execute(query)
            thread = result.scalar_one_or_none()
            if thread is None:
                raise Exception
            thread.messages.extend(messages)
            return thread

    async def update_thread(self, thread: orm.Thread) -> None:
        async with self._get_session() as session:
            await session.merge(thread)

    async def delete_threads(self, *, user_id: str, ids: list[str] | None) -> None:
        async with self._get_session() as session:
            if ids is None:
                single_page = await self._get_threads(
                    session, user_id=user_id, pagination=schema.Pagination.as_single_page()
                )
                threads = single_page.items
            else:
                threads = [await self._get_thread(session, user_id=user_id, id=id, with_messages=False) for id in ids]

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
