import pytest
from pydantic import ValidationError

from embry0.api.schemas import SandboxProfileRequest


def test_request_accepts_qa_fields():
    req = SandboxProfileRequest(
        name="qa-jvm",
        base_image="embry0-sandbox-qa:latest",
        description="JVM + Node QA",
        dind_enabled=True,
        idle_timeout_seconds=900,
        extra_networks=["sandbox-internet"],
        env_defaults={"LANG": "C.UTF-8"},
    )
    assert req.dind_enabled is True
    assert req.idle_timeout_seconds == 900


def test_request_defaults_preserve_legacy_callers():
    # No new fields supplied — should validate with safe defaults.
    req = SandboxProfileRequest(name="legacy")
    assert req.description == ""
    assert req.dind_enabled is False
    assert req.idle_timeout_seconds == 600
    assert req.extra_networks == []
    assert req.env_defaults == {}


def test_request_rejects_is_builtin_from_user():
    """Users cannot mark their custom profile as builtin via the API."""
    with pytest.raises(ValidationError):
        SandboxProfileRequest(name="hax", is_builtin=True)


def test_request_validates_idle_timeout_positive():
    with pytest.raises(ValidationError):
        SandboxProfileRequest(name="bad", idle_timeout_seconds=0)


def test_request_refuses_the_issue_553_probe():
    """Each field that would undo Ring 1 or the egress drop is refused, and named."""
    with pytest.raises(ValidationError) as caught:
        SandboxProfileRequest(
            name="probe",
            base_image="ghcr.io/example/root-image:latest",
            cap_drop=[],
            cap_add=["ALL"],
            security_opt=["seccomp=unconfined", "apparmor=unconfined"],
            extra_networks=["bridge"],
        )
    refused = {err["loc"][0] for err in caught.value.errors()}
    assert refused == {"base_image", "cap_add", "security_opt", "extra_networks"}, caught.value.errors()


@pytest.mark.parametrize(
    "fields",
    [
        {"cap_drop": ["NET_RAW"]},
        {"cap_add": ["SYS_ADMIN"]},
        {"security_opt": ["no-new-privileges", "seccomp=unconfined"]},
        {"security_opt": ["no-new-privileges=false"]},
        {"base_image": "docker.io/library/ubuntu:latest"},
        {"base_image": "registry:5000/embry0-sandbox:latest"},
        {"extra_networks": ["qa-net-job-other"]},
    ],
)
def test_request_refuses_each_weakening_field(fields):
    with pytest.raises(ValidationError) as caught:
        SandboxProfileRequest(name="weak", **fields)
    assert [err["loc"][0] for err in caught.value.errors()] == list(fields), caught.value.errors()


def test_request_keeps_the_safe_defaults():
    req = SandboxProfileRequest(name="plain")
    assert (req.cap_drop, req.cap_add, req.security_opt) == (["ALL"], [], ["no-new-privileges"])
    # An empty list means the default, as the repository stores it.
    req = SandboxProfileRequest(name="empty", cap_drop=[], security_opt=[], extra_networks=[])
    assert (req.cap_drop, req.security_opt) == ([], [])
