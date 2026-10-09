"""Pure ownership decision for the DinD volume reaper (#274)."""

from datetime import UTC, datetime, timedelta

import pytest

from embry0.cache.volume_ownership import (
    LABEL_INSTANCE,
    LABEL_JOB_ID,
    LABEL_MANAGED,
    LABEL_SCOPE,
    LABEL_SCOPE_KEY,
    VolumeInfo,
    VolumeReason,
    decide_volume,
    owning_job_id,
    volume_labels,
)
from embry0.storage.schemas import JobStatus

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=3)
GRACE = timedelta(hours=1)
INSTANCE = "a1b2c3d4e5f60718"


def _job_vol(job_id: str = "job-0a926d6c185e", *, created_at: datetime | None = OLD) -> VolumeInfo:
    return VolumeInfo(
        name=f"embry0-qa-vol-{job_id}",
        labels=volume_labels("per-job", job_id, instance_id=INSTANCE),
        created_at=created_at,
    )


def _decide(
    vol: VolumeInfo,
    statuses: dict[str, str],
    *,
    adopt_legacy_names: bool = False,
    instance_id: str | None = INSTANCE,
):
    return decide_volume(
        vol,
        instance_id=instance_id,
        job_statuses=statuses,
        now=NOW,
        unknown_owner_grace=GRACE,
        adopt_legacy_names=adopt_legacy_names,
    )


def test_volume_labels_per_job_carry_the_owner():
    assert volume_labels("per-job", "job-1", instance_id=INSTANCE) == {
        LABEL_MANAGED: "true",
        LABEL_INSTANCE: INSTANCE,
        LABEL_SCOPE: "per-job",
        LABEL_SCOPE_KEY: "job-1",
        LABEL_JOB_ID: "job-1",
    }


def test_volume_labels_per_repo_have_no_owning_job():
    labels = volume_labels("per-repo", "org/r1", instance_id=INSTANCE)
    assert labels[LABEL_SCOPE] == "per-repo"
    assert labels[LABEL_SCOPE_KEY] == "org/r1"
    assert LABEL_JOB_ID not in labels


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.COMPLETED,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
        JobStatus.PR_MERGED,
        JobStatus.PR_CLOSED,
    ],
)
def test_terminal_owner_is_removed(status):
    d = _decide(_job_vol(), {"job-0a926d6c185e": str(status)})
    assert d.remove is True
    assert d.reason is VolumeReason.OWNER_TERMINAL
    assert d.job_id == "job-0a926d6c185e"


@pytest.mark.parametrize(
    "status",
    [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.AWAITING_INPUT, JobStatus.PAUSED],
)
def test_live_owner_is_never_removed(status):
    d = _decide(_job_vol(), {"job-0a926d6c185e": str(status)})
    assert d.remove is False
    assert d.reason is VolumeReason.OWNER_ACTIVE


def test_unrecognised_status_is_kept():
    """A status this module has never heard of is treated as live."""
    d = _decide(_job_vol(), {"job-0a926d6c185e": "some_future_status"})
    assert d.remove is False
    assert d.reason is VolumeReason.OWNER_ACTIVE


def test_unknown_owner_past_grace_is_removed():
    d = _decide(_job_vol(), {})
    assert d.remove is True
    assert d.reason is VolumeReason.OWNER_UNKNOWN


def test_unknown_owner_inside_grace_is_kept():
    d = _decide(_job_vol(created_at=NOW - timedelta(minutes=5)), {})
    assert d.remove is False
    assert d.reason is VolumeReason.OWNER_UNKNOWN_IN_GRACE


def test_unknown_owner_without_a_creation_time_is_kept():
    d = _decide(_job_vol(created_at=None), {})
    assert d.remove is False
    assert d.reason is VolumeReason.OWNER_UNKNOWN_IN_GRACE


def test_foreign_volume_is_never_removed():
    vol = VolumeInfo(name="postgres-data", labels={"com.docker.compose.project": "qa_x"}, created_at=OLD)
    d = _decide(vol, {}, adopt_legacy_names=True)
    assert d.remove is False
    assert d.reason is VolumeReason.NOT_EMBRY0


def test_anonymous_volume_is_never_removed():
    vol = VolumeInfo(name="a" * 64, labels={}, created_at=OLD)
    assert _decide(vol, {}, adopt_legacy_names=True).reason is VolumeReason.NOT_EMBRY0


def test_per_repo_volume_is_never_removed():
    vol = VolumeInfo(
        name="embry0-qa-vol-org-r1", labels=volume_labels("per-repo", "org/r1", instance_id=INSTANCE), created_at=OLD
    )
    d = _decide(vol, {}, adopt_legacy_names=True)
    assert d.remove is False
    assert d.reason is VolumeReason.SHARED_SCOPE


def test_managed_volume_without_a_job_label_is_kept():
    vol = VolumeInfo(
        name="embry0-qa-vol-x",
        labels={LABEL_MANAGED: "true", LABEL_INSTANCE: INSTANCE, LABEL_SCOPE: "per-job"},
        created_at=OLD,
    )
    d = _decide(vol, {})
    assert d.remove is False
    assert d.reason is VolumeReason.NO_OWNER_LABEL


def test_legacy_unlabelled_volume_is_foreign_by_default():
    vol = VolumeInfo(name="embry0-qa-vol-job-0a926d6c185e", labels={}, created_at=OLD)
    d = _decide(vol, {"job-0a926d6c185e": "failed"})
    assert d.remove is False
    assert d.reason is VolumeReason.NOT_EMBRY0


def test_legacy_unlabelled_volume_is_adopted_by_name_when_enabled():
    vol = VolumeInfo(name="embry0-qa-vol-job-0a926d6c185e", labels={}, created_at=OLD)
    d = _decide(vol, {"job-0a926d6c185e": "failed"}, adopt_legacy_names=True)
    assert d.remove is True
    assert d.job_id == "job-0a926d6c185e"


def test_legacy_adoption_still_protects_a_live_owner():
    vol = VolumeInfo(name="embry0-qa-vol-job-0a926d6c185e", labels={}, created_at=OLD)
    d = _decide(vol, {"job-0a926d6c185e": "running"}, adopt_legacy_names=True)
    assert d.remove is False


@pytest.mark.parametrize(
    "name",
    [
        "embry0-qa-vol-org-r1",  # legacy per-repo volume
        "embry0-qa-vol-job-0a926d6c185e-extra",
        "embry0-qa-vol-job-XYZ",
        "embry0-qa-vol-job-0a926d6c185e\n",  # `$` alone would accept this
        "other-job-0a926d6c185e",
    ],
)
def test_legacy_adoption_matches_only_the_exact_per_job_name(name):
    vol = VolumeInfo(name=name, labels={}, created_at=OLD)
    assert owning_job_id(vol, adopt_legacy_names=True) is None
    assert _decide(vol, {}, adopt_legacy_names=True).reason is VolumeReason.NOT_EMBRY0


def test_volume_labels_without_an_instance_id_carry_no_instance_label():
    assert LABEL_INSTANCE not in volume_labels("per-job", "job-1", instance_id=None)


@pytest.mark.parametrize("statuses", [{}, {"job-0a926d6c185e": "failed"}])
def test_volume_of_another_instance_is_never_removed(statuses):
    """A second stack on the same daemon, or this stack booted against a
    different database: a missing jobs row there proves nothing here."""
    d = _decide(_job_vol(), statuses, instance_id="ffffffffffffffff")
    assert d.remove is False
    assert d.reason is VolumeReason.FOREIGN_INSTANCE


@pytest.mark.parametrize("statuses", [{}, {"job-1": "failed"}])
def test_labelled_volume_without_an_instance_label_is_never_removed(statuses):
    vol = VolumeInfo(
        name="embry0-qa-vol-job-1", labels=volume_labels("per-job", "job-1", instance_id=None), created_at=OLD
    )
    for adopt in (False, True):
        d = _decide(vol, statuses, adopt_legacy_names=adopt)
        assert d.remove is False
        assert d.reason is VolumeReason.FOREIGN_INSTANCE


def test_unresolved_instance_id_protects_every_labelled_volume():
    d = _decide(_job_vol(), {"job-0a926d6c185e": "failed"}, instance_id=None)
    assert d.remove is False
    assert d.reason is VolumeReason.FOREIGN_INSTANCE


def test_empty_instance_label_never_matches_an_empty_instance_id():
    vol = VolumeInfo(
        name="embry0-qa-vol-job-1",
        labels={**volume_labels("per-job", "job-1", instance_id=None), LABEL_INSTANCE: ""},
        created_at=OLD,
    )
    assert _decide(vol, {"job-1": "failed"}, instance_id="").reason is VolumeReason.FOREIGN_INSTANCE


def test_per_repo_volume_of_another_instance_reports_foreign_instance():
    vol = VolumeInfo(
        name="embry0-qa-vol-org-r1", labels=volume_labels("per-repo", "org/r1", instance_id="other"), created_at=OLD
    )
    assert _decide(vol, {}).reason is VolumeReason.FOREIGN_INSTANCE


def test_legacy_adoption_does_not_need_an_instance_id():
    """Unlabelled volumes predate instance labels; the explicit switch alone governs them."""
    vol = VolumeInfo(name="embry0-qa-vol-job-0a926d6c185e", labels={}, created_at=OLD)
    d = _decide(vol, {"job-0a926d6c185e": "failed"}, adopt_legacy_names=True, instance_id=None)
    assert d.remove is True
