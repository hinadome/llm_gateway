"""The versioned prompt library (prompt ops).

Keys can reference a *named* prompt (``VirtualKey.system_prompt_ref``)
instead of inline text; the library's **active** version serves, so
editing the prompt is a version bump + activate — no key change, no
restart, instant rollback. A second **candidate** version can A/B
against the active one: a fraction of the successful requests served
with the active version is mirrored to the candidate in the background
(the candidate never serves clients — see ``pipeline._queue_prompt_ab``),
with comparison rows in the request log.

Resolution is cached in-process (``prompts.library_cache_seconds``):
admin-API writes invalidate the entry immediately; the TTL is the
cross-replica backstop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import func, select

from ..db import get_sessionmaker
from ..models import Prompt


@dataclass(frozen=True)
class CandidatePrompt:
    """The A/B challenger of a resolved prompt."""

    version: int
    body: str
    fraction: float


@dataclass(frozen=True)
class ResolvedPrompt:
    """What the pipeline needs to serve (and optionally mirror) a
    library-referenced system prompt."""

    name: str
    version: int
    body: str
    candidate: CandidatePrompt | None


class PromptLibrary:
    """CRUD + cached resolution over the ``prompts`` table."""

    def __init__(self, cache_seconds: float = 30.0) -> None:
        self.cache_seconds = cache_seconds
        # name -> (resolved, monotonic expiry). Writes invalidate; the TTL
        # covers replicas that did not see the write.
        self._cache: dict[str, tuple[ResolvedPrompt, float]] = {}

    # ------------------------------------------------------------------
    # Admin-plane CRUD
    # ------------------------------------------------------------------

    async def create_version(self, name: str, body: str, notes: str | None) -> dict:
        """Append a new version. The first version of a name becomes
        active (an inactive-only prompt could never serve)."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            async with session.begin():
                highest = await session.scalar(
                    select(func.max(Prompt.version)).where(Prompt.name == name)
                )
                row = Prompt(
                    name=name,
                    version=(highest or 0) + 1,
                    body=body,
                    notes=notes,
                    # auto-activate only when no active version exists
                    is_active=not await session.scalar(
                        select(func.count()).where(
                            Prompt.name == name, Prompt.is_active.is_(True)
                        )
                    ),
                )
                session.add(row)
            await session.refresh(row)
        self.invalidate(name)
        return row.to_dict()

    async def list_names(self) -> list[dict]:
        """One summary per prompt name: active version, candidate, count."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(Prompt).order_by(Prompt.name, Prompt.version.desc())
                    )
                )
                .scalars()
                .all()
            )
        summaries: dict[str, dict] = {}
        for row in rows:
            summary = summaries.setdefault(
                row.name, {"name": row.name, "versions": 0, "active": None, "candidate": None}
            )
            summary["versions"] += 1
            if row.is_active:
                summary["active"] = row.to_dict()
            elif row.is_candidate:
                summary["candidate"] = row.to_dict()
        return list(summaries.values())

    async def versions(self, name: str) -> list[dict] | None:
        """All versions of one name, newest first (None = unknown name)."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(Prompt)
                        .where(Prompt.name == name)
                        .order_by(Prompt.version.desc())
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return None
        return [row.to_dict() for row in rows]

    async def patch_version(
        self,
        prompt_id: int,
        *,
        activate: bool | None = None,
        candidate: bool | None = None,
        ab_fraction: float | None = None,
        notes: str | None = None,
    ) -> dict | None:
        """Activate / mark-as-candidate / retarget a version.

        Activating one version deactivates its siblings (and uncandidates
        it — an active version is not also a challenger); candidating one
        uncandidates the previous challenger. Returns the patched row, or
        None when the id is unknown.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            async with session.begin():
                row = await session.get(Prompt, prompt_id)
                if row is None:
                    return None
                if activate:
                    await session.execute(
                        Prompt.__table__.update().where(
                            Prompt.name == row.name, Prompt.id != row.id
                        ).values(is_active=False)
                    )
                    row.is_active = True
                    if row.is_candidate:
                        row.is_candidate = False
                if candidate is not None:
                    if candidate:
                        await session.execute(
                            Prompt.__table__.update().where(
                                Prompt.name == row.name, Prompt.id != row.id
                            ).values(is_candidate=False)
                        )
                    row.is_candidate = candidate
                if ab_fraction is not None:
                    row.ab_fraction = min(1.0, max(0.0, ab_fraction))
                if notes is not None:
                    row.notes = notes
            await session.refresh(row)
        self.invalidate(row.name)
        return row.to_dict()

    async def delete_version(self, prompt_id: int) -> dict | tuple[str, dict] | None:
        """Delete one version. The active version of a name is refused
        (keys reference the name; deleting what serves them must be an
        explicit activate-something-else first). Returns the deleted
        row, ``("active", row)`` for the refusal, or None when the id
        is unknown."""
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            async with session.begin():
                row = await session.get(Prompt, prompt_id)
                if row is None:
                    return None
                if row.is_active:
                    return ("active", row.to_dict())
                snapshot = row.to_dict()
                await session.delete(row)
        self.invalidate(row.name)
        return snapshot

    # ------------------------------------------------------------------
    # Data-plane resolution (cached)
    # ------------------------------------------------------------------

    def invalidate(self, name: str) -> None:
        self._cache.pop(name, None)

    async def resolve(self, name: str) -> ResolvedPrompt | None:
        """The active version (and A/B candidate) of a name, or None when
        no active version exists. Cached for ``cache_seconds``; admin
        writes through this process invalidate immediately."""
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit is not None and hit[1] > now:
            return hit[0]

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            active = (
                (
                    await session.execute(
                        select(Prompt)
                        .where(Prompt.name == name, Prompt.is_active.is_(True))
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            resolved: ResolvedPrompt | None = None
            if active is not None:
                candidate_row = (
                    (
                        await session.execute(
                            select(Prompt).where(
                                Prompt.name == name, Prompt.is_candidate.is_(True)
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                candidate = (
                    CandidatePrompt(
                        version=candidate_row.version,
                        body=candidate_row.body,
                        fraction=candidate_row.ab_fraction,
                    )
                    if candidate_row is not None
                    else None
                )
                resolved = ResolvedPrompt(
                    name=name, version=active.version, body=active.body, candidate=candidate
                )
        self._cache[name] = (resolved, now + self.cache_seconds)
        return resolved
