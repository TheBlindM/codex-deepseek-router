import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(1, str(ROOT))

from runtime.client import DeepSeekClient, _structured, build_fallback_prompt
from runtime.context import TaskContext, sanitize_context
from runtime.protocol import ErrorCode, RouterError
from runtime.router import (
    FLASH_FIRST_PRO_DEMAND_THRESHOLD,
    RouteMode,
    Router,
    choose,
    resolve_policy,
)
import codex_deepseek_router as manager


def test_plugin_manifest_and_hook_are_relative_and_parseable():
    manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "codex-deepseek-router"
    assert manifest["skills"] == "./skills/"
    assert "hooks" not in manifest
    hook = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = hook["hooks"]["SubagentStart"][0]["hooks"][0]["command"]
    assert "$PLUGIN_ROOT" in command
    assert "/Users/" not in command and "C:\\\\" not in command


def test_repo_marketplace_points_to_local_plugin_with_icon():
    marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["name"] == "deepseek-router"
    entry = marketplace["plugins"][0]
    assert entry["name"] == "codex-deepseek-router"
    assert entry["source"] == {"source": "local", "path": "./"}
    plugin_root = (ROOT / entry["source"]["path"]).resolve()
    manifest = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert (plugin_root / manifest["interface"]["logo"]).is_file()


def test_setup_is_plugin_first(paths, fake_codex, no_credentials):
    manager.setup(paths, fake_codex, api_key_stdin=False, skip_live_test=True)
    assert not paths.hooks_config.exists()
    assert paths.plugin_hooks_config.is_file()
    status = manager.static_status(paths, fake_codex)
    assert status["plugin"]["manifest_valid"] is True
    assert status["hook"]["legacy"]["present"] is False


def test_legacy_migration_removes_only_owned_entry(paths, fake_codex, no_credentials):
    manager.setup(paths, fake_codex, api_key_stdin=False, skip_live_test=True)
    old = manager.our_hook_config(paths)
    old["hooks"]["UserPromptSubmit"] = [{"matcher": ".*", "hooks": [{"type": "command", "command": "echo keep"}]}]
    paths.hooks_config.write_text(json.dumps(old))
    result = manager.migrate_legacy(paths)
    assert result["status"] == "migrated"
    remaining = json.loads(paths.hooks_config.read_text(encoding="utf-8"))
    assert "SubagentStart" not in remaining["hooks"]
    assert "UserPromptSubmit" in remaining["hooks"]


def test_router_uses_explicit_mode_and_complexity_metadata():
    assert choose("flash", "review", {}).mode.value == "flash"
    assert choose("pro", "review", {}).mode.value == "pro"
    assert choose("auto", "architecture", {"file_count": 20, "context_size": 500000, "reasoning_depth": 9}).mode.value == "pro"


def test_auto_router_is_flash_first_for_moderate_read_only_work():
    context = {
        "file_count": 10,
        "context_size": 180000,
        "reasoning_depth": 6,
    }
    decision = choose("auto", "trace a moderately complex behavior", context)
    assert FLASH_FIRST_PRO_DEMAND_THRESHOLD == 0.80
    assert decision.score < FLASH_FIRST_PRO_DEMAND_THRESHOLD
    assert decision.mode is RouteMode.FLASH


def test_auto_router_keeps_pro_for_saturated_demand():
    context = {
        "file_count": 20,
        "context_size": 500000,
        "reasoning_depth": 9,
    }
    decision = choose("auto", "system-wide architecture decision", context)
    assert decision.score >= FLASH_FIRST_PRO_DEMAND_THRESHOLD
    assert decision.mode is RouteMode.PRO


def test_fallback_policy_defaults_are_deterministic():
    assert resolve_policy(RouteMode.FLASH, None) == "FAST"
    assert resolve_policy(RouteMode.PRO, None) == "REACT"


def test_fallback_rejects_flash_deep():
    with pytest.raises(RouterError) as error:
        resolve_policy(RouteMode.FLASH, "DEEP")
    assert error.value.code is ErrorCode.CONFIGURATION
    assert "requires deepseek_pro" in str(error.value)


@pytest.mark.parametrize(
    ("mode", "policy", "marker"),
    [
        ("flash", "FAST", "minimum direct evidence"),
        ("flash", "REACT", "precise read-only proposal"),
        ("pro", "REACT", "requested result"),
        ("pro", "DEEP", "material failure modes"),
    ],
)
def test_fallback_prompt_uses_reasoning_adapter(mode, policy, marker):
    prompt = build_fallback_prompt(mode, policy, "TASK\ninspect")
    assert f"POLICY\n{policy}" in prompt
    assert "POLICY EXECUTION CONTRACT" in prompt
    assert "CONVERGENCE / STOP CONDITION" in prompt
    assert "explicit text-only fallback" in prompt
    assert "VISUAL_QUALITY_UNVERIFIED" in prompt
    assert marker in prompt
    assert prompt.index("TASK CONTEXT") < prompt.index("POLICY EXECUTION CONTRACT")
    assert prompt.index("CONVERGENCE / STOP CONDITION") < prompt.index("OUTPUT FORMAT")


def test_fallback_ablation_omits_only_flash_tuning():
    contract_only = build_fallback_prompt(
        "flash", "FAST", "TASK\ninspect", guidance_variant="contract_only"
    )
    full = build_fallback_prompt(
        "flash", "FAST", "TASK\ninspect", guidance_variant="contract_tuning"
    )
    assert "MODEL-SPECIFIC TUNING" not in contract_only
    assert "MODEL-SPECIFIC TUNING" in full
    assert "supplied evidence directly" in full


def test_fallback_pro_has_no_generic_model_tuning():
    contract_only = build_fallback_prompt(
        "pro", "SPEC", "TASK\ninspect", guidance_variant="contract_only"
    )
    full = build_fallback_prompt(
        "pro", "SPEC", "TASK\ninspect", guidance_variant="contract_tuning"
    )
    assert contract_only == full
    assert "MODEL-SPECIFIC TUNING" not in full
    assert "supplied evidence directly" not in full


def test_fallback_flash_spec_requests_structured_evidence_packet_only_for_spec():
    spec = build_fallback_prompt(
        "flash", "SPEC", "TASK\ninspect", guidance_variant="contract_tuning"
    )
    fast = build_fallback_prompt(
        "flash", "FAST", "TASK\ninspect", guidance_variant="contract_tuning"
    )
    assert "escalate_to_pro" in spec
    assert "recommended_next_step" in spec
    assert "escalate_to_pro" not in fast


def test_router_passes_policy_to_selected_client():
    calls = {}

    class Client:
        def complete(self, task, **kwargs):
            calls.update(kwargs)
            return {"status": "ok"}

    result = Router(lambda mode: Client()).route("inspect", {}, "flash", policy="SPEC")
    assert result["status"] == "ok"
    assert calls["policy"] == "SPEC"


def test_context_sanitizer_withholds_attachments():
    assert "withheld" in sanitize_context({"image": "/tmp/secret.png"})
    assert "withheld" in sanitize_context({"metadata": {"image": "base64-data"}})
    shared = {"value": "ok"}
    assert sanitize_context([shared, shared]).count("value") == 2
    rendered = TaskContext("inspect", {"a.py": "x"}, visual_context={"observations": ["button"]}).render("flash")
    assert "parent Codex" in rendered
    assert "original image" in rendered


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({
            "output_text": json.dumps({"summary": "ok", "findings": [], "confidence": 0.9}),
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }).encode()


def test_client_returns_structured_result(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    client = DeepSeekClient("flash", opener=lambda request, timeout: FakeResponse())
    result = client.complete("inspect", context={"file_count": 1})
    assert result["status"] == "completed"
    assert result["model"] == "deepseek-v4-flash"
    assert result["confidence"] == 0.9
    assert result["usage"]["input_tokens"] == 3


def test_client_uses_persisted_base_url_and_model_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    settings_dir = tmp_path / "deepseek-router"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": "http://127.0.0.1:8001/openai/v1",
                "flash_model": "v4-flash-0731",
                "pro_model": "v4-pro-0813",
            }
        ),
        encoding="utf-8",
    )
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        return FakeResponse()

    result = DeepSeekClient("flash", opener=opener).complete("inspect")
    assert result["model"] == "v4-flash-0731"
    assert seen["url"] == "http://127.0.0.1:8001/openai/v1/responses"


def test_structured_extracts_json_after_provider_preamble():
    value = _structured('I should return JSON only.\n{"summary":"ok","findings":[]}')
    assert value == {"summary": "ok", "findings": []}


def test_client_normalizes_object_fields_without_exposing_preamble(monkeypatch):
    class PreambleResponse(FakeResponse):
        def read(self):
            return json.dumps({
                "output_text": (
                    'I should answer now.\n'
                    '{"summary":"ok","findings":{"timeout":12},'
                    '"evidence":{"observed":{"source":"cli"}}}'
                )
            }).encode()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    result = DeepSeekClient("flash", opener=lambda request, timeout: PreambleResponse()).complete(
        "inspect"
    )
    assert result["summary"] == "ok"
    assert result["findings"] == [{"timeout": 12}]
    assert result["evidence"]["observed"] == [{"source": "cli"}]
    assert "I should" not in json.dumps(result)


def test_client_preserves_structured_flash_escalation(monkeypatch):
    class EscalationResponse(FakeResponse):
        def read(self):
            return json.dumps({
                "output_text": json.dumps({
                    "summary": "needs deep concurrency reasoning",
                    "escalate_to_pro": True,
                    "evidence_packet": {
                        "schema": 1,
                        "summary": "conflicting lease snapshot",
                        "relevant_files": ["renew.py", "settle.py"],
                        "observations": ["fields are read and written separately"],
                        "hypotheses": ["torn logical snapshot"],
                        "eliminated": [],
                        "open_questions": ["atomicity boundary"],
                        "recommended_next_step": "verify the storage transaction boundary",
                    },
                })
            }).encode()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    result = DeepSeekClient(
        "flash", opener=lambda request, timeout: EscalationResponse()
    ).complete("inspect", policy="SPEC")
    assert result["escalate_to_pro"] is True
    assert result["evidence_packet"]["schema"] == 1
    assert result["evidence_packet"]["recommended_next_step"]


def test_flash_spec_fallback_builds_packet_when_provider_omits_it(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    result = DeepSeekClient(
        "flash", opener=lambda request, timeout: FakeResponse()
    ).complete("inspect", policy="SPEC")
    packet = result["evidence_packet"]
    assert result["escalate_to_pro"] is True
    assert set(packet) == {
        "schema",
        "summary",
        "relevant_files",
        "observations",
        "hypotheses",
        "eliminated",
        "open_questions",
        "recommended_next_step",
    }
    assert packet["summary"] == "ok"


def test_client_maps_missing_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("runtime.client._credential_from_manager", lambda: None)
    with pytest.raises(RouterError) as error:
        DeepSeekClient("pro", opener=lambda *args: FakeResponse()).complete("inspect")
    assert error.value.code is ErrorCode.AUTH


def test_hook_invalid_json_fails_open(tmp_path):
    script = ROOT / "hooks" / "plaintext_handoff.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--mode", "hook", "--state-directory", str(tmp_path)],
        input="not-json",
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["hookSpecificOutput"]["additionalContext"] == ""


def test_hook_non_string_agent_type_fails_open(tmp_path):
    script = ROOT / "hooks" / "plaintext_handoff.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--mode", "hook", "--state-directory", str(tmp_path)],
        input=json.dumps({"hook_event_name": "SubagentStart", "agent_type": []}),
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["hookSpecificOutput"]["additionalContext"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"hooks": []},
        {"hooks": {"SubagentStart": [None]}},
    ],
)
def test_malformed_plugin_hook_shape_is_unavailable(tmp_path, payload):
    class PluginPaths:
        plugin_hooks_config = tmp_path / "hooks.json"

    PluginPaths.plugin_hooks_config.write_text(json.dumps(payload))
    assert manager.plugin_hook_available(PluginPaths()) is False
