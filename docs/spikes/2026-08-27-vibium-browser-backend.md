# Spike: Vibium as a second QA browser backend

**Date:** 2026-08-27 · **Branch:** `vibium-integration` · **Status:** spec agreed, implementation pending

## Summary

Add [Vibium](https://github.com/VibiumDev/vibium) (Jason Huggins' WebDriver-BiDi browser automation tool, Apache-2.0) as a second **browser backend** for the QA agent, selectable per repo/app via qa.yaml v2 with Playwright remaining the default. A backend is the pluggable unit that supplies the QA agent's browser: an MCP stdio server config, a tool allowlist, an auth-injection mechanism, and a prompt fragment. Nothing changes for existing repos.

Why: Vibium's MCP surface (85 tools) is broader than Playwright MCP on element mapping (`browser_map` `@ref`s, a11y tree), state management (storage export/restore as tools), waiting, and recording (Playwright-compatible trace ZIPs) — and it's a single ~10 MB Go binary speaking the W3C BiDi standard, giving us a second, independently-implemented engine to compare QA reliability against.

## Locked decisions

| # | Decision | Choice | Key rationale |
|---|---|---|---|
| 1 | Switch surface | `browser_backend: playwright \| vibium` in qa.yaml v2 at all three layers (`defaults` → `app` → `app_local`), merged into `ResolvedAppConfig`; `QAJobOverrides.browser_backend` per-job escape hatch; deployment default in `config.py`. Default `playwright` everywhere. | Matches how everything else in QA is configured; per-repo/per-app control; A/B without redeploys. |
| 2 | Console/network diagnostics gap | Evaluate-shim + traces: Vibium MCP has **no** console/network tools, so the Vibium prompt fragment instructs the agent to install a console/fetch recorder via `browser_evaluate` after each navigation and to capture `browser_record_*` traces as artifacts. | Near-parity failure evidence; traces are Playwright-viewer-compatible ZIPs that slot into the existing `traces` artifact kind and 50 MiB cap. |
| 3 | Image strategy | **Single image** — Vibium coexists with Playwright inside `embry0-sandbox-qa`. | ~10 MB binary + ~200 MB Chrome-for-Testing on an image that already ships Chromium; no new profiles, registry wiring, or capability-gate refactor; the `"embry0-sandbox-qa" in base_image` checks keep working unchanged for both backends. |
| 4 | Version policy | Pin **`vibium@26.8.21`** exactly (npm); `vibium install` at image build bakes the matching Chrome-for-Testing. | CalVer + nightlies make `@latest` drift-prone (the `@playwright/mcp@latest` chromium-revision scar in this very Dockerfile); hard floor is 26.8.21 anyway — it fixes storage restore (#217: pre-26.8.21 silently dropped local/sessionStorage), a pipe-transport hang directly relevant to agent harnesses, and adds checksum-verified downloads. Upgrades are a deliberate one-line bump, same style as `claude-code@2.1.92`. |
| 5 | Tool allowlist | Curated **49 of 85** tools (full list below; the ticket texts say "45" — a tally slip corrected during #233, the itemized list is authoritative). | Exact-name allowlist parity with the 22-tool Playwright list; 85 schemas is context weight and attack surface. |
| 6 | Auth injection | **Prompt-driven restore via tool call** — but see the 2026-08-29 correction below: the ordering is **navigate → restore → reload** (not restore-first), and the orchestrator **converts** the Playwright storageState to vibium's native shape in `auth_setup_node`. A login page persisting after the reload is an auth *failure*, not an invitation to log in. | Vibium has no env-var equivalent of `PLAYWRIGHT_MCP_STORAGE_STATE`; restore is a tool call, and the browser lives inside the agent's MCP process, so the orchestrator can't do it. Failure mode is benign (login wall + evidence). **Correction (2026-08-29, #234), verified against the pinned 26.8.21 binary:** (a) restore applies web storage ONLY from vibium's native single-origin shape `{cookies, storage: {origin, localStorage: {k:v}, sessionStorage: {k:v}}}` — Playwright's `origins[]` silently restores cookies-only; (b) web-storage restore requires the browser to BE on the app's origin — restoring before first navigation throws SecurityError. Hence: `auth_setup_node` converts in place for vibium runs (`to_vibium_storage_state`, single-origin — the `frontend_url` match wins, extra origins dropped with a warning), and the fragment mandates start → navigate → restore → navigate-again. Residual risk: an app that hard-redirects off-origin (external IdP) before restore breaks the localStorage leg — the target-repo pilot is the test. |
| 7 | Boot-failure screenshot | `workflows/qa/screenshot.py` becomes **backend-aware**: Playwright path unchanged; Vibium path drives the `vibium` CLI (`go` + `screenshot --full-page`, headless, `VIBIUM_CHROME_ARGS=--no-sandbox`). | Keeps a future Playwright-free QA image possible. |
| 8 | grok/xAI executor | **Supported at launch.** Generalize `mcp_client.py`'s Playwright-specific `required: []` schema normalization to all MCP servers; grok+Vibium proof job required. | Vibium is stdio, so the transport fits; "never defer issues" — complete the matrix now. |
| 9 | Engine scope | **Chrome-for-Testing only.** Firefox (Vibium's second engine) is not baked; `browser_start(engine: firefox)` errors self-explanatorily. A future `engine:` qa.yaml field is cleanly additive. | Parity with the Chromium-only Playwright path; no cross-browser requirement today; ~250 MB saved. |
| 10 | Spec home | This document + a tracking GitHub issue with the phase checklist. | Follows the `docs/spikes/` precedent (`2026-07-23-emb54-xai-native-sdk.md`). |
| 11 | Glossary | `docs/glossary.md` is the single glossary — no root `CONTEXT.md`. `docs/agents/domain.md` + CLAUDE.md pointers updated accordingly. | Two glossaries fork the vocabulary. |
| 12 | Proof targets | Managed-mode fixture proof jobs for the 3-way matrix (Claude+Playwright regression, Claude+Vibium, grok+Vibium) **gate the merge**; **target-repo** deployed-target pilot with real Auth0 storage-state runs post-merge. | Hermetic matrix for CI-adjacent gating; only a real login wall proves restore-first. |

## How Playwright integrates today (the four layers a backend must supply)

1. **Image**: `infra/Dockerfile.sandbox.qa` — Chromium runtime libs (L24-28), `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright` (L45), `npm install -g @playwright/mcp@latest playwright@latest` (L54), Chromium pre-download via MCP's *bundled* playwright-core (L56-67).
2. **MCP server + allowlist**: `embry0/workflows/qa/agent_seed.py:186-202` (`playwright-mcp --headless --browser chromium`, stdio) and the 22-name `mcp__playwright__*` allowlist (L149-181), duplicated byte-for-byte in `embry0/storage/repositories/agent_definitions.py:77-129` (sync enforced by test).
3. **Orchestrator-side boot screenshot**: `embry0/workflows/qa/screenshot.py` (hard-codes the bundled playwright-core path).
4. **Auth storageState contract**: `embry0/workflows/qa/_subtask_env.py:71-81` (`PLAYWRIGHT_MCP_STORAGE_STATE` + `PLAYWRIGHT_MCP_ISOLATED=1`), validated at `subtask_nodes.py:533`; env keys reserved in `embry0/execution/auth_provider.py:53-57`.

Config flow: DB `qa` agent row → `qa_node` (`workflows/qa/nodes.py:376-395`) → resolver (`agents/resolver.py:226-230`, where `pipeline_config["mcp_servers"][agent_type]` already overrides) → `--config` JSON into the sandbox → `sandbox/runner.py` (Ring-3 policy sync) → `agents/config_builder.py` (SDK options).

## Vibium facts that shape the design

- **v26.8.21** (2026-08-22, CalVer): single Go binary, WebDriver BiDi, Chrome-for-Testing auto-managed via `vibium install`. MCP: `vibium mcp` — **stdio only**, `--headless` flag (26.8.21+), `--screenshot-dir` configurable. 85 tools served (the CLI `--help` on older builds lists 22 — frozen help text, upstream #393; the live `tools/list` is authoritative).
- **Root in DinD**: Chrome refuses to sandbox as root and Vibium deliberately errors rather than auto-fixing (upstream #141) → must inject `VIBIUM_CHROME_ARGS=--no-sandbox`.
- **No egress at job time** → Chrome-for-Testing must be baked at image build (`vibium install`), cache pinned via `VIBIUM_CACHE_DIR`.
- **No dedicated proxy option** — `VIBIUM_CHROME_ARGS=--proxy-server=…` is the only route, if ever needed.
- **Storage state**: ~~`browser_restore_storage` restores cookies + local/sessionStorage from Playwright's `{cookies, origins}` shape (fixed in 26.8.21)~~ — **wrong, corrected 2026-08-29 (#234)**: the pinned 26.8.21 binary restores web storage only from its native single-origin shape, only while on-origin (see decision 6's correction). State files written 0644 (unfixed upstream) — irrelevant in-sandbox but noted.
- **Gaps vs Playwright MCP**: no console/network tools (→ decision 2), no batch form-fill, no find-by-description.
- **Extras vs Playwright MCP**: `browser_map`/`browser_diff_map` `@ref` element maps, a11y tree, storage tools, clock mocking (excluded — below), trace recording, viewport/media/geo emulation.

## The 49-tool Vibium allowlist

Surfaced to the SDK as `mcp__vibium__<name>`; Ring-3 policy syncs automatically from the invocation.

- **Session/nav (7):** `browser_start`, `browser_stop`, `browser_navigate`, `browser_back`, `browser_reload`, `browser_get_url`, `browser_get_title`
- **Interaction (12):** `browser_click`, `browser_dblclick`, `browser_fill`, `browser_type`, `browser_press`, `browser_hover`, `browser_check`, `browser_uncheck`, `browser_select`, `browser_scroll`, `browser_drag`, `browser_upload` — auto-waiting actionability
- **Reading/locating (8):** `browser_find`, `browser_find_all`, `browser_get_text`, `browser_get_attribute`, `browser_is_visible`, `browser_map`, `browser_diff_map`, `browser_a11y_tree`
- **Waiting (4):** `browser_wait`, `browser_wait_for_url`, `browser_wait_for_load`, `browser_wait_for_text`
- **Eval/capture (2):** `browser_evaluate` (required by the shim), `browser_screenshot`
- **Recording (6):** `browser_record_start`, `browser_record_stop`, `browser_record_start_group`, `browser_record_stop_group`, `browser_record_start_chunk`, `browser_record_stop_chunk`
- **Tabs/frames (5):** `browser_new_page`, `browser_list_pages`, `browser_switch_page`, `browser_frames`, `browser_frame`
- **Viewport (2):** `browser_set_viewport`, `browser_get_viewport`
- **Dialogs (2):** `browser_dialog_accept`, `browser_dialog_dismiss`
- **State (1):** `browser_restore_storage`

**Excluded (36) and why:** raw mouse ×4 (coordinate clicking bypasses actionability), clock mocking ×8 (lets the agent fake its way past time-dependent acceptance criteria and masks real timing bugs), `browser_set_content` (rewrites the page under test — evidence tampering), cookie read/write/delete + `browser_storage_state` export ×4 (session-state exfiltration; auth flows through restore only), `browser_sleep` (flaky timing), `browser_wait_for_fn`/`browser_keys`/`browser_focus`/`browser_scroll_into_view`/`browser_count`/`browser_get_html`/`browser_get_value`/`browser_is_enabled`/`browser_is_checked`/`browser_highlight`/`browser_forward`/`browser_close_page`/`browser_pdf`/`browser_download_set_dir`/`browser_get_window`/`browser_set_window`/`browser_emulate_media`/`browser_set_geolocation` (marginal, covered by granted tools, or out of evidence scope).

Note: `browser_evaluate` is as powerful as Playwright's ToolSearch-denied `browser_run_code_unsafe`, but the Playwright list grants `browser_evaluate` too and the shim strategy requires it.

## Architecture

### New module: `embry0/workflows/qa/browser_backends.py`

Frozen-dataclass registry, direct analogue of `agents/providers.py`:

```python
@dataclass(frozen=True)
class BrowserBackend:
    name: str                      # "playwright" | "vibium"
    mcp_server_config: dict        # the stdio server entry
    tool_names: tuple[str, ...]    # exact mcp__<server>__* names
    env: dict                      # backend-specific sandbox env (e.g. VIBIUM_CHROME_ARGS)
    storage_state_env: dict        # auth env contract (playwright: the 2 PLAYWRIGHT_MCP_* keys; vibium: none — restore is a tool call)
    prompt_fragment: str           # appended to the QA system prompt
PLAYWRIGHT = BrowserBackend(...)   # today's exact values, verbatim
VIBIUM = BrowserBackend(
    name="vibium",
    mcp_server_config={"vibium": {"type": "stdio", "command": "vibium",
        "args": ["mcp", "--headless", "--screenshot-dir", "/workspace/.qa/screenshots"]}},
    ...)
def backend_for(name: str) -> BrowserBackend: ...
```

The Vibium `prompt_fragment` carries: restore-first auth protocol (decision 6), the evaluate-shim recipe (install a console/error/fetch recorder after every navigation; read it back on failure), trace recording habits (`browser_record_start` at session start, `browser_record_stop` → upload as `traces` artifact), `browser_map`-first element targeting, and "engine is Chrome; do not pass `engine: firefox`".

### Config plumbing

- `qa_yaml_v2.py`: `browser_backend: Literal["playwright", "vibium"] = "playwright"` on `DefaultsBlock`, `AppEntry`, `AppLocalConfig` (`extra="forbid"` means the field must exist before any repo can set it).
- `qa_yaml_resolve.py`: three-layer merge into `ResolvedAppConfig.browser_backend`.
- `api/schemas/__init__.py`: `QAJobOverrides.browser_backend` (mirrors `sandbox_profile`); override precedence: job override > app_local > app > defaults > `config.py` deployment default (`qa_browser_backend_default = "playwright"`).
- `subtask_state.py`: resolved backend rides into job.json (`browser_backend` field) so the agent knows its own mode.

### Injection point

`_with_browser_backend(definition, backend)` helper applied in `qa_node` (`workflows/qa/nodes.py:376-395`) — merged copy of the DB agent definition with `mcp_servers` replaced, `tools` rewritten (non-`mcp__*` tools kept; browser tool names swapped for `backend.tool_names`), and `prompt_fragment` appended. The DB seed and `BUILTIN_SEED` stay Playwright-shaped and untouched — same pattern as `generic_agent._with_watcher_log_tool`. For `backend.name == "playwright"` the helper is a no-op (byte-identical definition), guaranteeing zero regression risk.

### Env & reservation

- `_subtask_env.py`: emit `backend.env` + `backend.storage_state_env`; on Vibium that's `VIBIUM_CHROME_ARGS=--no-sandbox`, `VIBIUM_CACHE_DIR=<baked path>`, and `QA_STORAGE_STATE_PATH` (already backend-agnostic).
- `auth_provider.py`: add `VIBIUM_CHROME_ARGS`, `VIBIUM_CACHE_DIR`, `VIBIUM_SESSION`, `VIBIUM_CONNECT_URL`, `VIBIUM_CONNECT_API_KEY`, `VIBIUM_ENGINE_PATH`, `VIBIUM_ENGINE_VERSION`, `VIBIUM_ENGINE_CHANNEL` to `RESERVED_ENV_KEYS`.

### Image (`infra/Dockerfile.sandbox.qa`)

```dockerfile
ENV VIBIUM_CACHE_DIR=/opt/vibium
RUN npm install -g vibium@26.8.21 \
    && vibium install \
    && chmod -R a+rwX /opt/vibium   # non-root `agent` user needs write (profile dirs), same as /opt/playwright
```

### xAI path

`agents/mcp_client.py`: generalize the `required: []` normalization from playwright-only to all servers (harmless broadly). `executor_xai.py` unchanged (stdio already supported).

### Backend-aware boot screenshot

`workflows/qa/screenshot.py`: dispatch on the sub-task's resolved backend; Vibium variant execs `vibium --headless go <url>` + `vibium screenshot --full-page -o <path>` with `VIBIUM_CHROME_ARGS=--no-sandbox`, then `vibium daemon stop`.

## Phases

**Phase 1 — registry + switch (no Vibium runtime yet):** `browser_backends.py`; qa.yaml field + resolve + `ResolvedAppConfig`; `QAJobOverrides` + config default; `_with_browser_backend` in `qa_node`; job.json field; unit tests proving the playwright path is byte-identical to today.

**Phase 2 — Vibium runtime:** Dockerfile changes; env plumbing + reserved keys; prompt fragment (restore-first, shim, traces); `mcp_client.py` normalization; backend-aware `screenshot.py`; qa-yaml corpus fixture with `browser_backend: vibium`.

**Phase 3 — validation + docs:** update affected tests (`test_agent_seed.py`, `test_orchestrator.py`, `test_subtask_env.py`, `test_screenshot_helper.py`, `test_mcp_client.py`, `test_executor_xai.py`, corpus fixtures); CI smoke gains `vibium --version` beside `playwright --version` (`.github/workflows/ci.yml:100-118`); frontend `mcp_servers` placeholder note (`AgentFormPage.tsx:361`); docs (`architecture.md`, `running-qa.md`, `qa-yaml-reference.md`, `providers.md`, `glossary.md`, CLAUDE.md image notes); **merge gate:** managed-fixture proof jobs — Claude+Playwright (regression), Claude+Vibium, grok+Vibium; **post-merge pilot:** target-repo deployed target, Claude+Vibium with Auth0 storage-state (proves restore-first against a real login wall).

## Risks & watch items

- **Evaluate-shim fidelity**: the recorder only sees `console.*`/`fetch`/`XMLHttpRequest` after injection — it misses pre-injection boot errors and non-fetch network (websockets, workers). Accepted; traces partially compensate. Revisit when upstream adds console/network MCP tools.
- **xAI schema validation** of Vibium's 49 schemas is unverified until the grok+Vibium proof job runs; fix-forward in `mcp_client.py` if it trips.
- **Nightly drift**: "85 tools" verified against 26.8.21-era `main`; the pin insulates us, but a future bump must re-verify the 49 allowlisted names exist.
- **Restore-first is prompt-enforced, not harness-enforced**: an agent that skips it hits a login wall and fails visibly — benign, but expect prompt iteration during the pilot.
- **Playwright packages must stay in the image** while any repo defaults to playwright (they may be dropped in a future Vibium-only image now that `screenshot.py` is backend-aware — that's what decision 7 buys).
