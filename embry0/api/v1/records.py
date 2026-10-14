"""Per-repo records — Markdown documents with full revision history.

Documents about one registered repository that must never live in a git tree:
RCAs, audits, runbooks, deployment-specific specs and plans. Reads are open to
every authenticated tier, writes to operators, deletes to admins. A key
limited to a repository allowlist reaches only the records of the
repositories on its list, reads included. Design:
docs/superpowers/specs/2026-10-14-repo-records-design.md.

Every route is a ``{full_repo:path}`` sub-resource, so this router must be
included before the ``repos`` router's catch-all (tests/unit/api/
test_repos_route_order.py). The slug is a single segment, so a path such as
``/repos/group/sub/project/records/outage`` parses unambiguously.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from embry0.api.deps import request_actor, require_admin
from embry0.api.repo_allowlist import enforce_repo_allowlist
from embry0.storage.repositories.repo_records import (
    MAX_LIST_LIMIT,
    MAX_TITLE_CHARS,
    RecordError,
    RepoRecordsRepository,
)

router = APIRouter()


class RecordPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    body: str
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    source_job_id: str | None = Field(default=None, max_length=200)


def _records(request: Request) -> RepoRecordsRepository:
    repo: RepoRecordsRepository = request.app.state.repo_records_repo
    return repo


def _http(exc: RecordError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/repos/{full_repo:path}/records")
async def list_records(
    full_repo: str,
    request: Request,
    kind: str | None = None,
    q: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=200, ge=1, le=MAX_LIST_LIMIT),
) -> dict[str, Any]:
    await enforce_repo_allowlist(request, full_repo, action="read records")
    try:
        rows = await _records(request).list_records(full_repo, kind=kind, query=q, limit=limit)
    except RecordError as exc:
        raise _http(exc) from exc
    return {"repo": full_repo, "records": rows}


@router.get("/repos/{full_repo:path}/records/{slug}/revisions")
async def list_record_revisions(full_repo: str, slug: str, request: Request) -> dict[str, Any]:
    await enforce_repo_allowlist(request, full_repo, action="read records")
    revisions = await _records(request).revisions(full_repo, slug)
    if revisions is None:
        raise HTTPException(status_code=404, detail=f"no record {slug!r} for {full_repo}")
    return {"repo": full_repo, "slug": slug, "revisions": revisions}


@router.get("/repos/{full_repo:path}/records/{slug}")
async def get_record(
    full_repo: str, slug: str, request: Request, revision: int | None = Query(default=None, ge=1)
) -> dict[str, Any]:
    await enforce_repo_allowlist(request, full_repo, action="read records")
    row = await _records(request).get(full_repo, slug, revision=revision)
    if row is None:
        what = f"revision {revision} of record {slug!r}" if revision else f"record {slug!r}"
        raise HTTPException(status_code=404, detail=f"no {what} for {full_repo}")
    return row


@router.put("/repos/{full_repo:path}/records/{slug}")
async def put_record(
    full_repo: str, slug: str, req: RecordPutRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Create the record, or append a revision when its title or body changed.

    201 on create, 200 otherwise; ``changed`` is false when the stored title
    and body were identical and no revision was added.
    """
    await enforce_repo_allowlist(request, full_repo, action="write records")
    try:
        row = await _records(request).put(
            full_repo,
            slug,
            kind=req.kind,
            body=req.body,
            title=req.title,
            author=request_actor(request),
            source_job_id=req.source_job_id,
        )
    except RecordError as exc:
        raise _http(exc) from exc
    if row["created"]:
        response.status_code = 201
    return row


@router.delete(
    "/repos/{full_repo:path}/records/{slug}", status_code=204, response_model=None, dependencies=[require_admin]
)
async def delete_record(full_repo: str, slug: str, request: Request) -> None:
    """Delete a record and every revision. Admin only: history is the point of the store."""
    await enforce_repo_allowlist(request, full_repo, action="delete records")
    if not await _records(request).delete(full_repo, slug):
        raise HTTPException(status_code=404, detail=f"no record {slug!r} for {full_repo}")
