"""Seven jobs shaped like the ones that died with ERR_QA_FAILURES_UNRESOLVED (#298).

The changed-file lists are synthetic but keep the shape of the real ones: the
same directory layout, file kinds and mix of frontend, backend, docs and infra
paths per job. Each real job was approved by the reviewer and then failed
because QA ran against the deployed frontend, which did not contain the branch.
Under the tiers none of them reaches QA.
"""

from __future__ import annotations

import pytest

from embry0.workflows.verification.config import TierRules
from embry0.workflows.verification.tiers import classify_diff, plan_browser_qa

P = "platform/api/src/main/java/com/acme/shop_api/"
T = "platform/api/src/test/java/com/acme/shop_api/"
L = "frontend/apps/orders/src/"

JOBS: dict[str, list[str]] = {
    "job-0000000000a1": [
        L + "__tests__/DashboardCharts.test.tsx",
        L + "api/orders.ts",
        L + "components/ActionCards.tsx",
        L + "components/DashboardView.tsx",
        L + "components/ChartCard.tsx",
        L + "components/ui.tsx",
        L + "types.ts",
    ],
    "job-0000000000a2": [
        "frontend/apps/pdftools/src/__tests__/ToolsViewBatch.test.tsx",
        "frontend/apps/pdftools/src/api/pdfTools.ts",
        "frontend/apps/pdftools/src/components/ToolsView.tsx",
        "frontend/apps/pdftools/src/pdf/pageCount.ts",
        "infra/docker-compose.yml",
        "infra/ocr/app.py",
        P + "controllers/ToolsController.java",
        P + "proxy/OcrProxyClient.java",
        P + "services/PdfConversionService.java",
        P + "services/PdfToolsService.java",
        T + "controllers/ToolsControllerTest.java",
        T + "services/PdfToolsRawOpsTest.java",
        T + "services/PdfToolsServiceTest.java",
        "platform/common/src/main/java/com/acme/shop_api/configs/CorsConfig.java",
    ],
    "job-0000000000a3": [
        L + "__tests__/CustomerTimeline.test.tsx",
        L + "api/orders.ts",
        L + "components/CustomerPage.tsx",
        P + "controllers/CustomerController.java",
        P + "services/CustomerTimelineService.java",
        P + "services/impl/CustomerTimelineServiceImpl.java",
        T + "services/CustomerTimelineServiceTest.java",
    ],
    "job-0000000000a4": [
        L + "__tests__/AccountsView.test.tsx",
        L + "__tests__/ContactCopyAndSource.test.tsx",
        L + "components/AccountsView.tsx",
        L + "components/CustomerDrawer.tsx",
        L + "components/CustomersListView.tsx",
        L + "components/CustomerPage.tsx",
    ],
    "job-0000000000a5": [
        L + "__tests__/SavedItemsPanel.test.tsx",
        L + "components/SavedItemsPanel.tsx",
    ],
    "job-0000000000a6": [
        P + "services/OrderService.java",
        P + "services/OrderExpiryService.java",
        P + "services/impl/OrderServiceImpl.java",
        P + "services/impl/OrderExpiryServiceImpl.java",
        "platform/api/src/main/resources/application.properties",
        T + "services/OrderExpiryServiceImplTest.java",
    ],
    "job-0000000000a7": [
        "docs/adr/0002-portal-accounts-as-a-second-realm.md",
        "docs/specs/2026-01-01-portal-foundation.md",
        "infra/docker-compose.yml",
        P + "integrations/sso/SsoOrgClient.java",
        P + "portal/PortalAccountService.java",
        "platform/api/src/main/resources/application.properties",
        T + "integrations/sso/SsoOrgClientTest.java",
        T + "portal/PortalAccountServiceWriteTest.java",
    ],
}

DEFAULT_TIER = {
    "job-0000000000a1": "frontend",
    "job-0000000000a2": "frontend",
    "job-0000000000a3": "frontend",
    "job-0000000000a4": "frontend",
    "job-0000000000a5": "frontend",
    "job-0000000000a6": "backend_only",
    # backend + docs + infra/docker-compose.yml: the compose file matches no
    # built-in rule, so the stricter tier applies until the repo claims it.
    "job-0000000000a7": "frontend",
}


def test_all_seven_jobs_are_covered() -> None:
    assert set(JOBS) == set(DEFAULT_TIER) and len(JOBS) == 7


@pytest.mark.parametrize("job_id", sorted(JOBS))
def test_tier_under_the_built_in_rules(job_id: str) -> None:
    assert classify_diff(JOBS[job_id]).tier == DEFAULT_TIER[job_id]


@pytest.mark.parametrize("job_id", sorted(JOBS))
def test_none_of_them_would_have_been_gated_on_the_deployed_site(job_id: str) -> None:
    """The repo's QA config declares one app, `web`, target: deployed."""
    tier = classify_diff(JOBS[job_id]).tier
    plan = plan_browser_qa(tier=tier, needs_qa=True, managed_apps=[], deployed_apps=["web"])
    assert plan.run_browser_qa is False
    assert plan.human_merge_only is (tier == "frontend")


def test_a_repo_rule_for_infra_makes_the_portal_fix_backend_only() -> None:
    rules = TierRules(backend_paths=["platform/**", "ml/**", "infra/**"])
    assert classify_diff(JOBS["job-0000000000a7"], rules).tier == "backend_only"
