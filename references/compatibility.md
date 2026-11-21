# Compatibility

## Supported platforms

| Platform | Credential backend | Agent TOML auth | Verified live smoke |
|---|---|---|---|
| macOS | Keychain (Security.framework) | same-Python Keychain helper (generated at install) | Native `0.148.0`; `direct_codex` Desktop `0.149.0-alpha.4` |
| Windows | Credential Manager | `env_key = "DEEPSEEK_API_KEY"` | Utopia-V baseline: Windows Desktop `26.727.6591.0` |
| Linux | `DEEPSEEK_API_KEY` env var | `env_key` | protocol/static tests; live smoke recommended |

- Python 3.9+ (stdlib only; no third-party runtime dependencies).
- Codex Desktop or CLI at least started once; the manager uses the desktop
  bundled runtime when discoverable (`CODEX_DESKTOP_BIN` overrides).

## Configuration layout

Default `CODEX_HOME` is `~/.codex`:

- Agent files: `$CODEX_HOME/agents/deepseek-flash.toml`,
  `$CODEX_HOME/agents/deepseek-pro.toml` — the DeepSeek provider lives
  **inside** these files. The top-level `config.toml` is never modified by
  the installer; the parent model/provider/login stay untouched.
- Dual-model registry: `$CODEX_HOME/models.json` (both
  `deepseek-v4-flash` and `deepseek-v4-pro`; never one without the other).
  The `model_catalog_json` pointer in `config.toml` is **not** changed.
- Plugin manifest: `<plugin-root>/.codex-plugin/plugin.json`.
- Plugin Hook: `<plugin-root>/hooks/hooks.json`, with `PLUGIN_ROOT`-relative scripts.
- Explicit staging helper: `$CODEX_HOME/hooks/codex-deepseek-router/` (not a
  registered Hook; removed by manager uninstall).
- Manager state, backups, manifest and handoff state:
  `$CODEX_HOME/deepseek-router/`.
- Credential target: `io.github.codex-deepseek-router.deepseek-api-key`
  (macOS Keychain / Windows Credential Manager).

## Tested baselines

| Component | Evidence |
|---|---|
| Codex CLI | Native: `0.148.0`; Desktop compatibility: `0.149.0-alpha.4` |
| Windows Codex Desktop | `26.727.6591.0` (Utopia-V live baseline) |
| DeepSeek models | `deepseek-v4-flash`, `deepseek-v4-pro`, Responses API |
| Transport | ≤0.148: V2 + `fork_turns="none"` + plaintext Hook; ≥0.149: `direct_codex` |
| Parent isolation | installer never writes config.toml (stronger than oil-oil's V1 workaround, which we deliberately do not copy) |

The manager selects transport from the detected Desktop runtime. Codex 0.149
preserves the parent provider for native children, so the Router starts a
bounded top-level execution with session-only DeepSeek provider overrides via
the same Desktop-bundled Codex binary. The assignment is sent through stdin;
the API key is resolved by command auth and never enters argv. Older compatible
versions retain native `spawn_agent` plus plaintext Hook delivery.

Each `direct_codex` delegation uses a temporary isolated `CODEX_HOME`. This
keeps the child execution usable when Codex Desktop runs shell tools under a
sandbox identity that can read but not write the primary `$CODEX_HOME`. The
temporary home carries only that bounded execution's state and is removed
afterward; it does not replace the user's parent model, provider, login,
configuration, or persistent session database.

Compatibility is proven by the live `test` command. Both paths require the
correct provider, model, role and a random challenge marker; the Native path
also validates callback/thread metadata. Flash passing never implies Pro
passing.

## Known boundaries

- **Codex 0.149+ cross-provider children**: `direct_codex` is a separate
  top-level Codex execution whose result is bridged back by the Router. It is
  not displayed as a native child in the current Desktop task and does not use
  `spawn_agent`/wait callbacks. The Router, not Codex, bounds ordinary
  assignments at 900 seconds and Complex Pro + REACT assignments carrying the
  required standalone `QUALITY CLOSURE` section at 1800 seconds. A positive
  `--delegate-timeout <seconds>` overrides the per-assignment default.

- **Windows Desktop + Credential Manager command auth**: a command-backed
  HKCU lookup fails under the Desktop sandbox identity
  (Utopia-V issue #6). Therefore Windows agents use `env_key`; the manager
  stores the key in Credential Manager and injects it into `codex exec`
  smoke runs. For Desktop use, set `DEEPSEEK_API_KEY` as a user environment
  variable and fully restart the Codex app.
- **Hook trust**: Codex owns Plugin Hook discovery and trust. The manager only
  reads public Hook metadata and reports whether the Plugin Hook is trusted;
  `/hooks` is fallback UI only.
- **Legacy hooks**: `migrate` removes only an exact entry whose existing script
  bytes match this Plugin. Foreign matchers are left untouched.
- **macOS Python identity**: setup records the exact Python executable in the
  generated agent auth block. Run `repair` after changing Python installations
  so Keychain reads continue under the identity that owns the item.
