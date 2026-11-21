<p align="center">
  <img src="./assets/readme/hero-en.svg" width="100%" alt="codex-deepseek-router keeps Codex as the parent, routes work to DeepSeek Flash or Pro, and verifies the native callback">
</p>

<p align="center">
  <img src="./assets/icon.png" width="180" alt="codex-deepseek-router icon">
</p>

<p align="center"><a href="README.md">简体中文</a> · <strong>English</strong></p>

<p align="center">
  <a href="https://github.com/TheBlindM/codex-deepseek-router/actions/workflows/ci.yml"><img src="https://github.com/TheBlindM/codex-deepseek-router/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
</p>

> Codex always remains the parent agent. It selects `deepseek_flash` or
> `deepseek_pro`, uses native children on compatible older versions or the
> Desktop-bundled `direct_codex` transport on 0.149+, then verifies and
> integrates the result itself.

## The outcome first

| Parent stays intact | Two specialized agents | Evidence, not self-report |
| --- | --- | --- |
| Never touches `config.toml`; the parent model, provider, and ChatGPT login stay unchanged | Flash handles fast read-only exploration; Pro handles deep reasoning and implementation | Provider, model, thread, and random challenge evidence must match the selected transport |

This is not a daemon, proxy, or MCP server, and it does not require a separate
CLI installation. Compatible versions through 0.148 use native children;
0.149+ uses the Desktop-bundled Codex binary for one bounded top-level
DeepSeek execution and bridges the result back to the parent.

## Quick start

Requirements: Node.js/npm, Python 3.9+, either ChatGPT Desktop (Codex) started
at least once or Codex CLI installed, and a DeepSeek API key.

### 1. Install the Plugin

#### ChatGPT Desktop (recommended)

If you only installed ChatGPT Desktop, you do not need a global `codex` command
in your system terminal. Open Codex in Desktop, start a new task, and send:

```text
Install this plugin:
https://github.com/TheBlindM/codex-deepseek-router
```

The Codex agent inspects `.agents/plugins/marketplace.json` in the repository
and starts the installation. When the plugin confirmation page appears, select
**Install plugin**. After installation, fully quit the app with `Cmd-Q` on
macOS (or exit it completely on Windows), reopen it, and start a new task.

Seeing `command not found: codex` in a system terminal is expected when only
Desktop is installed; it does not prevent the Desktop agent from installing
the plugin. You can also find **DeepSeek Router** in the Plugins page and
install it manually.

#### Codex CLI

Use these commands only when `codex --version` succeeds in your system
terminal:

```bash
codex plugin marketplace add TheBlindM/codex-deepseek-router
codex plugin add codex-deepseek-router@deepseek-router
```

`deepseek-router` is the real Marketplace name declared in
`.agents/plugins/marketplace.json`; it is not a placeholder.

The Plugin provides the management Skill, routing Skill, and native Hook. It
does not write a Hook into `~/.codex/hooks.json`.

#### Update or uninstall

| Environment | Update | Uninstall |
| --- | --- | --- |
| ChatGPT Desktop | Give the GitHub URL above to the Codex agent again and ask it to update and reinstall the plugin, or uninstall and reinstall it from the Plugins page | Open the plugin under Plugins → Installed and select uninstall |
| Codex CLI | Run `codex plugin marketplace upgrade deepseek-router`, then `codex plugin add codex-deepseek-router@deepseek-router` | Run `codex plugin remove codex-deepseek-router@deepseek-router` |

After installation or an update, fully restart Desktop/CLI and open a new task
so the new skills, hooks, and tools are loaded. See the
[OpenAI Plugins documentation](https://learn.chatgpt.com/docs/plugins) for the
general plugin workflow.

### 2. Configure it inside Codex

Restart Codex, open a new task, and ask:

```text
Install and configure codex-deepseek-router for me.
```

The Skill checks current state first. If no credential exists, Codex asks for
the API key and passes it to the manager through stdin only; the key never
enters command arguments, configuration files, or chat echo.

### 3. Review and verify

1. Restart Codex or start a new task and review/trust the Hook in Codex's native
   Plugin Hook UI.
2. Ask Codex to run the live router test; Flash and Pro must pass separately.
3. If the review prompt is not shown by the current Codex version, use `/hooks`
   in the interactive CLI as a fallback.

Then use it naturally:

```text
Use the DeepSeek agents to review this repository.
```

## How it works

```text
User task
   │
   ├─ Modality gate: TEXT_ONLY / VISION_TRANSLATABLE / VISION_CRITICAL
   ├─ Sensitivity gate: secrets and sensitive context stay in Codex
   ├─ Model router: Flash / Pro / no delegation
   └─ Policy router: FAST / REACT / SPEC / DEEP
            │
            ▼
runtime detection
   ├─ ≤0.148: stage → SubagentStart hook → native DeepSeek child
   └─ ≥0.149: Desktop-bundled Codex → top-level DeepSeek (`direct_codex`)
            │
            ▼
provider + model + thread + marker verification → Codex integration
```

### Who does what

| Target | Best for | Boundary |
| --- | --- | --- |
| `deepseek_flash` | Search, enumeration, logs, extraction, code maps, high-volume reading | Read-only; returns change proposals instead of editing files |
| `deepseek_pro` | Root cause, architecture, concurrency, security, complex review, cross-module implementation | Workspace-write; lands work that needs deep reasoning |
| Codex parent | Trivial work, sensitive context, critical visual judgment, final verification and integration | Always keeps control |

Routing is Flash-first. Large context, multi-file search, long logs, initial
root-cause analysis, and pre-implementation research do not become Pro work
merely because they are large. Pro is used for workspace edits, explicit
Flash escalation or demonstrated failure, high-risk reasoning, and
system-wide decisions that cannot be bounded with Flash evidence. The parent
may land a Flash read-only proposal directly without a redundant Pro pass.

Flash can return `ESCALATE_TO_PRO` with an Evidence Packet so Pro continues
from collected evidence instead of scanning the repository again. FAST / REACT
/ SPEC / DEEP are bounded decision contracts, not extra models or runtimes.

Router optimizes for useful convergence, not minimum runtime. A delegated
assignment should state `ACCEPTANCE CRITERIA`, `VERIFICATION OWNER`, and a
`STOPPING CONDITION`. REACT must satisfy the material criteria the child can
verify and surface parent-owned criteria such as screenshot or final visual
quality checks; “the page runs” is not acceptance by itself. The parent checks
the actual artifact and evidence. It issues at most one bounded `Pro + REACT`
follow-up only for an objective material engineering gap affecting correctness,
required user-visible behavior, integration, robustness, regression risk,
security/invariants, or concrete maintainability. Subjective polish and
unrelated refactoring do not trigger follow-up. Without visual evidence, the
result is `UNVERIFIED`, never an invented quality pass.

For an explicitly Complex Pro implementation, the assignment may require one
bounded `QUALITY CLOSURE`: after the first relevant functional verification
passes, inspect integration, edge/failure paths, and regression risks, fix only
material issues, and rerun the affected verification. Ordinary Pro work remains
fast and does not inherit this requirement.

On the Codex 0.149+ `direct_codex` path, this boundary also selects the per-run
limit: ordinary assignments default to 900 seconds, while a
`deepseek_pro + REACT` assignment containing a standalone `QUALITY CLOSURE`
section automatically receives 1800 seconds. A positive
`delegate --delegate-timeout <seconds>` value explicitly overrides that choice,
and the returned `timeout_seconds` records the effective limit. This is a Router
safety bound, not an inherent 15-minute Codex limit.

Each `direct_codex` delegation uses a temporary isolated `CODEX_HOME`. This
keeps the child execution usable when Codex Desktop runs shell tools under a
sandbox identity that can read but not write the primary `.codex` state
database. The temporary home is removed afterward and does not replace the
parent model, provider, login, configuration, or persistent session state.

This information-driven, acceptance-criteria-driven completion adds no fifth
policy, acceptance-profile schema field, or transport state machine; ordinary
functional work remains fast.

On the child's first turn, a shared Reasoning Adapter composes a policy
execution contract and stop condition, plus a short reminder for Flash to use
supplied evidence, obey output and honesty constraints, and explore only when
a fact is missing. Pro has no generic model anchor by default. Native Hook delivery and explicit no-Hook
fallback use the same adapter. Flash+DEEP is rejected before a pending file is
created and checked again when an envelope is read. Fallback has prompt parity,
not native child tool capability parity; its Flash SPEC result is conservatively
normalized into a complete Evidence Packet for Pro, while Native delivery keeps
conditional escalation.

Sixteen Golden Execution Tasks use A (Current), B (Contract Only), and C
(Contract + Model Tuning) ablations. Flash and Pro are scored separately, and
guidance is retained only when correctness does not regress and public token,
latency, duplicate-read, environment-check, unbounded-search, or parent-rework
metrics show a stable benefit. The router incorporates selected DSH-derived
behavioral findings while preserving Codex-native orchestration. DSH runtime
system/tool-surface rewriting is not emulated where Codex exposes no equivalent
control; no weak/mixed secondary router or same-role fan-out is introduced.

### Native acceptance and API ablation are separate paths

| Path | Entry point | Proves | Does not prove |
| --- | --- | --- | --- |
| Native Codex | `stage → SubagentStart hook → spawn_agent → callback`, or `scripts/codex_deepseek_router.py test --json` | Native child routing, Hook delivery, callback, thread metadata, markers, and real tool behavior | It is not the standalone prompt A/B harness |
| Standalone API eval | `scripts/run_execution_eval.py --live` | Reasoning Adapter prompt ablations, public token usage, latency, and answer rubric | It has no native tool/callback trace and is not a Native Pro failure rate |

`run_execution_eval.py --live` directly calls `DeepSeekClient(...).complete()` so
the A/B/C prompts can be controlled. Its timeout and retry settings belong only
to the fallback client; they do not configure or represent the Codex Native Agent
runtime. `NETWORK` or timeout results from this path must therefore be labeled as
standalone eval evidence, not as a Native Pro service failure. Release acceptance
uses the Native Codex smoke after the Hook has been reviewed and trusted.

## Installed components and safety boundaries

The manager:

- installs `deepseek-flash.toml` and `deepseek-pro.toml` together;
- registers both models in `~/.codex/models.json`;
- exposes `skills/` and `hooks/hooks.json` from the Plugin; Hook commands use
  `PLUGIN_ROOT` and never depend on a user's cwd or absolute paths;
- configures only credentials, Agents, the model catalog, and explicit fallback runtime;
- stores the key in the system credential store and rolls back every managed change on failure;
- never changes the parent's `config.toml` or forges hook trust.

On macOS, the same Python executable identity reads and writes Keychain through
Security.framework. `status` and `doctor` check item existence without
decrypting the key or repeating authorization, and user-facing replies follow
the language of the user's current request.

DeepSeek children receive text only. Codex must translate screenshots, images,
and video into textual facts first; critical visual judgment stays with Codex.
On Windows, agents authenticate through the user-level `DEEPSEEK_API_KEY`
environment variable and require a full Codex restart after it is set.

## Manager commands

| Command | Purpose |
| --- | --- |
| `status` | Read-only runtime, agent, catalog, credential, and hook state |
| `setup` | Install every component idempotently and transactionally |
| `test` | Run independent native dispatch proofs for Flash and Pro |
| `repair` | Recover after parent-model upgrades, Codex updates, or drift |
| `migrate` | Precisely remove a legacy Skill-first global Hook without touching unrelated Hooks |
| `disable` | Record disabled intent; Plugin Hook lifecycle remains owned by Codex |
| `uninstall` | Remove project-owned content; keep the API key by default |
| `doctor` | Diagnose the environment, hook trust, and handoff state |

Every command accepts `--json` and `--codex-home`. Exit codes: `0` means
ready/configured, `2` means user action is required, `3` means timeout, and `1`
means an unexpected failure.

### Custom endpoints and model IDs

User configuration is persisted at `$CODEX_HOME/deepseek-router/settings.json`,
outside the Plugin cache. `base_url` is the API root and must not include
`/responses`; the router requests `{base_url}/responses` automatically.

```bash
python3 scripts/codex_deepseek_router.py setup \
  --base-url https://api.qnaigc.com/bypass/openai/v1 \
  --flash-model deepseek-v4-flash-0731 \
  --pro-model deepseek-v4-pro-0813
```

Local OpenAI-compatible gateway example:

```bash
python3 scripts/codex_deepseek_router.py setup \
  --base-url http://127.0.0.1:8001/openai/v1 \
  --flash-model v4-flash-0731 \
  --pro-model v4-pro-0813
```

The gateway must support the OpenAI Responses API. The router sends `POST
{base_url}/responses`.

Each option can be updated independently; omitted fields keep their saved values.
`repair` rebuilds the Agent TOMLs, `models.json`, and related runtime assets from
`settings.json` without restoring default endpoint or model IDs. Uninstalling the
Codex Plugin itself does not remove this settings file; `router uninstall` removes
it as part of the router's cleanup semantics.

<details>
<summary><strong>Install from source and verify manually</strong></summary>

```bash
git clone https://github.com/TheBlindM/codex-deepseek-router.git
cd codex-deepseek-router
python3 scripts/codex_deepseek_router.py status --json
```

Pass the key through stdin only:

```bash
printf '%s\n' '<your-key>' | python3 scripts/codex_deepseek_router.py setup --api-key-stdin --json
```

After Codex's native Plugin Hook review (or the CLI `/hooks` fallback), run the live proof:

```bash
python3 scripts/codex_deepseek_router.py test --json
```

`test` proves the expected `model_provider`, model, and agent role for each
child and verifies an independent random marker. Flash passing never implies
Pro passing.

</details>

## Documentation

- [Architecture](references/architecture.md) ·
  [routing policy](references/routing-policy.md) ·
  [multimodal boundary](references/multimodal.md)
- [Compatibility](references/compatibility.md) ·
  [security](references/security.md) ·
  [troubleshooting](docs/troubleshooting.md)
- [Architecture decisions](docs/architecture.md) · [DSH alignment decisions](docs/design-decisions.md) · [evaluation](docs/eval.md) ·
  [upstream source map](docs/upstream-reference-map.md)

## Development and verification

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
python3 scripts/run_execution_eval.py --variant all --smoke
```

Tests cover manager lifecycle and rollback, the handoff protocol, cross-role
isolation, router contracts, schema validation, and credential-leak scanning.
CI runs on Windows, macOS, and Linux across Python 3.9/3.11/3.12. Live
DeepSeek calls intentionally stay out of CI.

## Acknowledgements

- [oil-oil/codex-deepseek-subagent](https://github.com/oil-oil/codex-deepseek-subagent)
  established the manager, transactional rollback, system credentials,
  runtime discovery, and native verification foundation.
- [Utopia-V/codex-deepseek-subagent](https://github.com/Utopia-V/codex-deepseek-subagent)
  provided the foundation for the plaintext `SubagentStart` handoff transport.
- [yjh051108/dsh-routing-suite](https://github.com/yjh051108/dsh-routing-suite)
  and [dsh-router-standard](https://github.com/yjh051108/dsh-router-standard)
  inspired model-specific first-turn anchoring, convergence guidance, and
  per-model evaluation. This project does not adopt their runtime/injector or
  retracted internal-model theories.

Thank you to their authors and contributors for publishing implementation,
experiments, and design reasoning. Exact adaptations, source mapping, and
license notices live in [NOTICE.md](NOTICE.md) and the
[upstream source map](docs/upstream-reference-map.md).

## License

MIT — see [LICENSE](LICENSE).
