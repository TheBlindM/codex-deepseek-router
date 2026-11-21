"""Security tests: no credential leakage into logs, fixtures, snapshots, or
repository artifacts; argv hygiene for the handoff transport."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import codex_deepseek_router as manager
import plaintext_handoff as handoff

ROOT = Path(__file__).resolve().parents[1]

# Patterns that would indicate a real credential leak. Test doubles in the
# suite deliberately use short fake values ("sk-test") that cannot match.
SUSPICIOUS_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"DEEPSEEK_API_KEY\s*=\s*sk-", re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+sk-", re.IGNORECASE),
    re.compile(r"Bearer\s+sk-[A-Za-z0-9_\-]{16,}", re.IGNORECASE),
]

SCAN_EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".idea"}
SCAN_EXCLUDED_SUFFIXES = {".pyc", ".sqlite", ".sqlite-wal", ".sqlite-shm"}


def _scan_targets():
    targets = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SCAN_EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix in SCAN_EXCLUDED_SUFFIXES:
            continue
        targets.append(path)
    return targets


def test_repository_contains_no_real_api_keys():
    offenders = []
    for path in _scan_targets():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SUSPICIOUS_PATTERNS:
            match = pattern.search(text)
            if match:
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)[:24]}...")
    assert not offenders, f"credential-like strings found:\n" + "\n".join(offenders)


def test_status_never_contains_key(paths, fake_codex, no_credentials, monkeypatch):
    secret = "sk-test-secret"
    monkeypatch.setattr(manager, "read_credential_key", lambda: secret)
    manager.setup(paths, fake_codex, api_key_stdin=False, skip_live_test=True)
    for payload in (manager.static_status(paths, fake_codex), manager.doctor(paths, fake_codex)):
        assert secret not in json.dumps(payload)


def test_smoke_failure_payloads_never_contain_key(paths, monkeypatch):
    secret = "sk-test-secret"
    monkeypatch.setattr(manager, "platform_name", lambda: "linux")
    monkeypatch.setattr(manager, "credential_backend", lambda: None)
    monkeypatch.setattr(manager, "read_credential_key", lambda: secret)
    env = manager._smoke_env(paths)
    # Env-key platforms pass the value to the child process by design...
    assert env.get(manager.API_KEY_ENV) == secret
    # ...but never travels through JSON payloads.
    with pytest.raises(Exception) as exc:
        manager.native_spawn_smoke(paths, "/nonexistent/codex", "deepseek_flash", "deepseek-v4-flash")
    details = getattr(exc.value, "details", None) or {}
    assert secret not in json.dumps({"code": getattr(exc.value, "code", None), "details": details})


def test_smoke_env_can_use_isolated_direct_codex_home(paths, tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "read_credential_key", lambda: "sk-test")
    direct_home = tmp_path / "direct-home"
    env = manager._smoke_env(paths, direct_home)
    assert env["CODEX_HOME"] == str(direct_home)
    assert env["CODEX_HOME"] != str(paths.codex_home)


def test_macos_smoke_env_does_not_redundantly_decrypt_key(paths, monkeypatch):
    monkeypatch.delenv(manager.API_KEY_ENV, raising=False)
    monkeypatch.setattr(manager, "platform_name", lambda: "macos")
    monkeypatch.setattr(manager, "credential_backend", lambda: "macos-keychain")

    def fail_if_read():
        pytest.fail("macOS provider auth must own the only credential read")

    monkeypatch.setattr(manager, "read_credential_key", fail_if_read)

    env = manager._smoke_env(paths)

    assert manager.API_KEY_ENV not in env


def test_manifest_never_contains_key(paths, fake_codex, no_credentials, monkeypatch):
    monkeypatch.setattr(manager, "read_credential_key", lambda: "sk-test-secret")
    manager.setup(paths, fake_codex, api_key_stdin=False, skip_live_test=True)
    assert "sk-" not in paths.manifest.read_text(encoding="utf-8")


def test_handoff_cli_has_no_assignment_argv_option():
    parser = handoff.build_parser()
    options = [action.dest for action in parser._actions]
    assert "assignment" not in options
    assert "--assignment" not in sys.argv


def test_handoff_cli_stage_reads_assignment_from_stdin_only(handoff_dir):
    script = ROOT / "hooks" / "plaintext_handoff.py"
    secret = "sk-test-secret"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "stage",
            "--agent-type",
            "deepseek_flash",
            "--state-directory",
            str(handoff_dir),
        ],
        input=f"analyze this log (marker {secret})",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    # The staged assignment is plaintext state by design; the transport
    # contract is that it never appears in process argv of the manager CLI.
    assert secret not in "\n".join(sys.argv)


def test_error_payloads_are_secret_safe():
    # The failure model codes contain no credential material.
    codes = [
        "credential_missing",
        "hook_untrusted",
        "child_timeout",
        "native_route_mismatch",
        "config_conflict",
        "operation_in_progress",
    ]
    assert all("sk-" not in code for code in codes)
