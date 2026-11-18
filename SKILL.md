---
name: codex-deepseek-router
description: Install, configure, check, test, repair, disable or uninstall the codex-deepseek-router suite — DeepSeek Flash/Pro roles, native and Desktop-compatible transports, and the runtime routing skill. Use only when the user asks to manage this router's installation. Do not trigger for ordinary DeepSeek API questions or everyday coding tasks.
---

# Codex DeepSeek Router

This skill maintains the router installation. It never carries out everyday
user tasks. Deterministic file, catalog, credential, hook and smoke-test
operations go through `scripts/codex_deepseek_router.py`; never hand-edit the
TOML/JSON agent files, `hooks.json`, the model catalog or system credential
stores.

## User-facing language

Write all commentary, questions, progress updates and final reports in the
same language as the user's latest request, unless the user explicitly asks
for another language. Treat English status codes and manager messages as
machine-readable inputs to translate, not as a reason to switch languages.

## Key contracts

- Codex stays the parent agent. The parent model, provider and persisted
  `config.toml` are never changed. Native roles carry their provider locally;
  `direct_codex` applies it only to the delegated top-level execution.
- Both agents are installed simultaneously:
  `deepseek_flash` → `deepseek-v4-flash` (fast worker) and
  `deepseek_pro` → `deepseek-v4-pro` (deep solver). There is no Flash/Pro
  model switching via repair.
- DeepSeek children are text-only. Never send original images, screenshots or
  video to them; the parent inspects visual inputs and passes text facts.
- Task delivery uses the Plugin-owned plaintext `SubagentStart` hook. Codex
  owns discovery, review and trust; the installer never writes global Hook
  configuration or forges trust.
- Everyday delegation follows `status.transport_mode`. Codex 0.149+ uses the
  manager's `delegate` command and the Desktop-bundled Codex runtime;
  compatible older versions stage once and call
  `spawn_agent(agent_type="deepseek_flash" | "deepseek_pro", fork_turns="none")`.
- Never silently substitute another model or role. A transport change may
  preserve the requested DeepSeek role, but its result must identify the
  actual transport explicitly.
- Route with a Flash-first bias. Use `deepseek_flash` as the default delegated
  role for bounded read-only investigation, including large multi-file
  investigations. Reserve `deepseek_pro` for workspace edits, explicit Flash
  escalation or failure, high-risk reasoning, and decisions that cannot be
  bounded with Flash evidence.
- When an escalated task requires Pro, Pro owns implementation, tests and
  technical acceptance for that scope. A Flash parent performs mechanical
  evidence checks and must not replace Pro's technical judgment with a weaker
  independent analysis.

## After triggering

1. Run `status --json` and continue from the structured state; never guess
   from file names.
2. First-time configuration runs `setup --json`. When no API key is present
   the command returns `credential_missing`: ask the user briefly for the
   key, then pass it only through stdin:
   `printf '%s\n' '<key>' | python3 <skill-dir>/scripts/codex_deepseek_router.py setup --api-key-stdin --json`.
   Never echo, restate or write the key anywhere.
3. `setup` does not run live smoke tests. Run `test --json` afterwards. On
   Codex 0.149+ it verifies both roles through `direct_codex`, including the
   provider, model, thread ID and random challenge marker. Older compatible
   versions verify native spawn, callback and child metadata. Flash passing
   never implies Pro passing — both must pass separately.
4. If the result carries `hook_review_required`, tell the user to restart or
   open a new task so Codex can show the native Plugin Hook review UI. The
   interactive CLI `/hooks` command is a fallback only when that UI is absent.
5. If the result carries `restart_required` or `new_task_required`, tell the
   user to restart the Codex desktop app and open a new task.
6. After a parent model upgrade or a Codex update, run `repair --json`.
7. Final reports state only status, provider, model, roles, hook trust state
   and backup location; never output keys or raw event logs.

## Commands

Entry point (`python3` on macOS/Linux, `py -3` on Windows):

```text
python3 <skill-dir>/scripts/codex_deepseek_router.py <command> --json
```

- `status` — read-only check of runtime, agents, catalog, credential, hook
  and parent isolation.
- `setup` — configure credentials, both agents and the dual-model catalog;
  Plugin Skills and Hooks are discovered by Codex and are never copied into
  global Hook configuration. Requires the API key via `--api-key-stdin` when missing.
- `test` — live dual-role smoke tests through the selected Desktop runtime transport.
- `delegate` — one bounded 0.149+ `direct_codex` execution; assignment is
  stdin-only. Ordinary work defaults to 900 seconds and Complex Pro + REACT
  work with the required standalone `QUALITY CLOSURE` section defaults to 1800
  seconds; `--delegate-timeout <seconds>` is a positive explicit override.
- `repair` — idempotently re-apply the managed configuration and refresh the
  recorded parent snapshot (use after parent model upgrades).
- `migrate` — remove only a precisely recognized legacy global Hook.
- `disable` — record that automatic routing is disabled; Plugin removal owns
  the Hook itself.
- `uninstall` — remove everything this project owns; the API key is kept
  unless `--remove-credential` is passed.
- `doctor` — diagnostics for the environment, hook trust and handoff state.

Use the current `CODEX_HOME` by default; pass `--codex-home` only when the
user explicitly manages another Codex home. On machines where the Codex
runtime is not discoverable, set `CODEX_DESKTOP_BIN` to the bundled `codex`
binary.

## Statuses

- `ready` — both live smokes passed for the reported transport and markers.
- `configured` — static configuration complete; live tests not run yet.
- `partial` / `not_installed` — read `errors` and the structured checks.
- `disabled` — routing hook removed; `repair` restores it.
- `credential_missing` — ask for the API key, then continue the flow.
- `operation_in_progress` — another configuration operation is running; wait
  and retry, never modify concurrently.
- `conflict` — report the conflicting file/field and wait for the user's
  decision; never overwrite silently.
- `hook_untrusted` — review the Plugin Hook in Codex; `/hooks` is fallback only.
- `unsupported` — report the missing capability; do not bypass it by hand.

More detail: [references/compatibility.md](references/compatibility.md),
[references/routing-policy.md](references/routing-policy.md),
[references/multimodal.md](references/multimodal.md).
