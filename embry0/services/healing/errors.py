"""Errors raised by the healing-case ledger (#278)."""


class CaseNotFoundError(LookupError):
    """No healing case has this id."""

    def __init__(self, case_id: str) -> None:
        super().__init__(f"healing case {case_id!r} not found")
        self.case_id = case_id


class CaseConflictError(ValueError):
    """The write contradicts what the case already records."""


class CaseReferenceError(ValueError):
    """The write points at a row (watcher run, issue, job, case) that does not exist."""
