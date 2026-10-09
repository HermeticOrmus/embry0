"""The healing-case lifecycle ledger (#278).

One record per finding, from detection to a healed verdict. Writers go
through :class:`~embry0.services.healing.service.HealingCaseService`; the
stage rules live in :mod:`~embry0.services.healing.lifecycle`.
"""
