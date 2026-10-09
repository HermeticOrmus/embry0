"""Running the API lifespan in a test must not blind later ``capture_logs`` (#569).

``lifespan()`` calls ``configure_structlog``, which installs a new processor
list with ``cache_logger_on_first_use=True``. A module logger that logged
after one such call caches the list that was live then; a second call
installs another list, and ``capture_logs`` only mutates the current one, so
the cached logger is invisible to every later capture in the run.
"""

import structlog
from structlog.testing import capture_logs

from embry0 import observability

# Module level, like every ``logger = structlog.get_logger()`` in embry0.
probe = structlog.get_logger("structlog_isolation_probe")


def test_a_logger_used_between_two_startups_stays_capturable() -> None:
    observability.configure_structlog()  # first lifespan in the run
    probe.info("warm_up")  # the module logger's first log call
    observability.configure_structlog()  # a second lifespan

    with capture_logs() as logs:
        probe.warning("after_second_startup")

    assert [entry["event"] for entry in logs] == ["after_second_startup"], (
        "capture_logs saw nothing from a module logger that first logged between two "
        "configure_structlog calls: the logger cached a processor list that capture_logs "
        "no longer mutates"
    )
