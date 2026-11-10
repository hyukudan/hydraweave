# Hydraweave redesign

Home is the first implementation increment on `ui/redesign`. The other six sections are visual proposals only. No backend changes or commits are part of this increment.

## Token system

| Role | Light value | Usage |
| --- | --- | --- |
| Ground | `#E9EEF3` | Main canvas |
| Surface | `#FFFFFF` | Cards |
| Raised surface | `#F3F6F9` | Machine headers and secondary controls |
| Ink / rail | `#0F1B2A` | Primary text / navigation background |
| Secondary ink | `#45566A` | Supporting text |
| Muted ink | `#526377` | Labels and metadata |
| Action / USB4 | `#2459D6` | Primary buttons and two USB4 threads |
| Action text / tint | `#1E4DBA` / `#E8EFFD` | Links / quiet information panels |
| Border / strong border | `#CCD6E1` / `#8192A6` | Outlines / neutral machine threads |
| Current GPU turn | `#C7770F` | Verified current holder band and vertical thread only |
| Current turn surface / ink | `#FFF0D8` / `#713E04` | Readable holder label |
| Rail ink / muted ink | `#F5F8FC` / `#B4C2D3` | Navigation text |

Titles use Bricolage Grotesque when locally available, then Segoe UI Variable Display, Trebuchet MS, and system sans-serif. Body text uses Instrument Sans when locally available, then Segoe UI Variable Text, Segoe UI, and system sans-serif. IDs use JetBrains Mono, Cascadia Code, and Consolas. No font files or external requests are added: the server CSP permits local assets only and shell tests explicitly reject external URLs in index.html.

Hero type scales from 28 to 48 px; the phone hero is 34 px. Primary card titles are 24–26 px, management card titles 18 px, body text 14 px. Supporting metadata is smaller. Spacing follows 4, 8, 12, 16, 20, 24, 28, 32 px, with a 22 px page gap. Cards have 14 px radii, controls 8 px, a 1 px border, and no shadow. Existing focus-visible outlines remain. Controls reach 44 px at phone width. Existing dark, true-black, density, and reduced-motion preferences remain available.

## Mockups

Seven generated raster images, 1536 × 1024, made with the built-in image generation tool. Home is the implementation reference. These are design examples, not observations or benchmark results. Amber in Home and GPU turns demonstrates a held state; runtime amber must come from current queue evidence.

| Section | File | Implementation order |
| --- | --- | --- |
| Home | [home.png](mockups/home.png) | 1 — implemented |
| Engines | [engines.png](mockups/engines.png) | 2 |
| GPU turns | [gpu-turns.png](mockups/gpu-turns.png) | 3 |
| Models | [models.png](mockups/models.png) | 4 |
| Benchmarks | [benchmarks.png](mockups/benchmarks.png) | 5 |
| Updates / Components | [updates-components.png](mockups/updates-components.png) | 6 |
| Settings | [settings.png](mockups/settings.png) | 7 |

The exact initial and correction prompts are in [PROMPTS.json](PROMPTS.json). No CLI/API fallback was used. A correction pass removed invented queue entries and fixed pair/replica labels.

## Home changes

- `hydraweave/web/static/pages/control-center.js`: adds a serving summary, the weave, engine catalog links, and next steps. Three vertical threads represent cluster1, cluster2, and the optional Windows RTX PRO 6000 PC. Two blue horizontal threads describe the configured USB4 connection (2 cables / 4 streams); LAN is a neutral cross-thread. Existing setup data, checklist, controller/router roles, configuration errors, lifecycle guidance, node actions, and router dialog remain below the overview.
- `hydraweave/web/static/pages/control-center.css`: implements weave geometry, catalog, summary strip, and responsive layouts. The machine list remains semantic and readable independently of decorative threads.
- `hydraweave/web/static/app.css`: adds reusable tokens and scopes the new light canvas and navy rail to Home. Other section surfaces retain their current styles.
- `hydraweave/web/static/app.js`: Home's idle status reads “No engine serving”, separately from “Status updated N s ago”. Age comes from the received devices timestamp. Other routes retain their current behavior.
- `hydraweave/web/static/index.html`: changes browser theme color to navy, keeping all shell IDs and local assets.
- `tests/test_home_design.py`: checks current/stale turn evidence, unconfirmed pair attempts, optional/excluded GPUs, serving states, escaping, setup controls, and passive rendering.

All existing routes, mutation endpoints, API helpers, and launch dialogs are retained. Home additionally reads the existing `turns` endpoint when that page is registered. Queue-read failures do not replace the setup page. Home adds no engine-start control, queue write, framework, or backend code.

Amber requires a fresh HTTP document, a non-stale node observation, a parsed and verified living holder, a turn within its cap, and confirmed pair evidence when supplied. Unknown, unconfirmed, expired, or stale evidence stays neutral. Optional RTX hardware is only linked when a non-excluded RTX PRO 6000 is observed. Missing observations are labeled. Configured USB4/LAN geometry does not claim verified health without observations. The PC ledger is not treated as proof of a verified node GPU ticket.

The implementation follows Home's hierarchy and signature while preserving full navigation and management functions. Mockup machine count, age, and held state become runtime values. System font fallbacks deliberately differ from generated typography.

## Next sections

Review and merge Home first. Then implement Engines, GPU turns, Models, Benchmarks, Updates / Components, and Settings, one reviewable section at a time. Use each mockup as a visual proposal and existing endpoint contracts as the behavioral authority. Distinguish pairs from two replicas; GLM-5.3 needs a preset until one is configured.

## Verification


- Focused Python suite: **147 passed, 3 skipped**. Command: `python -m pytest tests/test_web_api.py tests/test_web_security.py tests/test_shell_static.py tests/test_shell_prefs.py tests/test_frontend_review.py tests/test_setup_feature.py tests/test_engine_settings.py tests/test_web_shutdown.py tests/test_placement_ui.py tests/test_observe_pages.py tests/test_home_design.py -q`.
- Both modified JavaScript files pass `node --check`. The existing `tests/fixtures/frontend/polling.js` passes restart, cache eviction, failed/hanging timeline, ETA, and stop-rendering checks.
- Existing contrast check: **45 text/surface pairs passed** across light, dark, and true black. An additional **12 Home-specific pairs passed**, including rail text, blue actions, and light/dark holder bands; the lowest measured Home text pair is **5.28:1**.
- Real Chromium against the dedicated control-center HTTP fixture: **1440, 760, 390, and 320 px**, light and dark, no document overflow or browser exceptions. Verified workspace initialization, opening the node form, draft retention through refresh, native Escape, and focus movement from Manage nodes. Chromium needed local test-only renderer flags in this environment; no browser flags were added to the product.
- Inspected [desktop](verification/home-desktop.png), [phone weave](verification/home-phone.png), and [held-band example](verification/home-held-example.png) captures. The held-band capture uses controlled browser-side fixture documents; it is not a live queue observation. The ordinary captures show missing hardware evidence honestly.
- Broader exploratory run: **191 passed, 5 skipped, 8 failed, 5 setup errors**. Six failures require an optional router/runtime module that is not installed; two component tests encounter Windows permissions while deleting staged Git files. The five browser setup errors stem from renderer startup. The stock all-page smoke also reaches an existing fixture guard failure on `setup` (its fake environment refuses an unfaked process call), so it cannot certify every route here. These backend/fixture failures are outside the Home changes; no backend code was changed to accommodate them.
- The existing JSDOM walkthrough was attempted but cannot start because its test-only JSDOM dependency is absent. The real-browser checks above cover Home's core dialog behavior; they do not claim the entire DOM walkthrough or a screen-reader audit.
- Final text files were checked for raw NUL bytes; PNGs were checked as binary images. `git diff --check` passed. Changes remain uncommitted.
