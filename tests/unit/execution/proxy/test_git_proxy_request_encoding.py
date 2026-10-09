"""A gzipped git request body reaches the forge as one consistent body (prod regression).

Git gzips an upload-pack request body over 1 KiB and says so with
``Content-Encoding: gzip``. The proxy's aiohttp server decompresses request
bodies before the handler reads them, so the proxy must not forward the
client's ``Content-Encoding``/``Content-Length``: they describe the compressed
bytes, and what it forwards is the decompressed stream. The first deploy of
#521 forwarded them, and every fetch that negotiated (review and QA base
fetches) failed with HTTP 400 and git's "expected 'acknowledgments'".
"""

import gzip
import os

import pytest

from embry0.execution.proxy.git_proxy import create_git_proxy_app

from .git_forge import ADMIN, ProxyRig, RecordingUpstream

BEARER = "tok-" + "a" * 32
UPLOAD_PACK = "/git/acme/widgets.git/git-upload-pack"


@pytest.fixture
async def rig():
    upstream = RecordingUpstream(content_type="application/x-git-upload-pack-result", body=b"0008NAK\n")
    async with ProxyRig(create_git_proxy_app(github_token="", admin_token=ADMIN), upstream) as r:
        await r.enroll("s1", BEARER, git_host="127.0.0.1", git_username="x-access-token", github_token="ghp_X")
        yield r


@pytest.mark.parametrize("size", [2_000, 200_000])
async def test_a_gzipped_request_body_reaches_the_forge_intact(rig, size):
    payload = os.urandom(size // 2).hex().encode()[:size]
    resp = await rig.client.post(
        UPLOAD_PACK,
        data=gzip.compress(payload),
        headers={
            "Authorization": f"Bearer {BEARER}",
            "Content-Type": "application/x-git-upload-pack-request",
            "Content-Encoding": "gzip",
        },
    )
    assert resp.status == 200, f"a gzipped upload-pack request came back {resp.status}: {(await resp.text())[:200]}"
    seen = rig.upstream.seen[-1]
    assert seen.body == payload, (
        f"the forge got {len(seen.body)} bytes ({seen.headers.get('Content-Encoding')!r} encoding, "
        f"Content-Length {seen.headers.get('Content-Length')!r}), not the {size}-byte body git sent"
    )


async def test_a_plain_request_body_is_forwarded_unchanged(rig):
    payload = b"0032want " + b"a" * 40 + b"\n00000009done\n"
    resp = await rig.client.post(
        UPLOAD_PACK,
        data=payload,
        headers={"Authorization": f"Bearer {BEARER}", "Content-Type": "application/x-git-upload-pack-request"},
    )
    assert resp.status == 200
    assert rig.upstream.seen[-1].body == payload
