"""Plaintext handoff protocol tests (dual-role port of the Utopia-V suite)."""

import datetime
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import plaintext_handoff as handoff

FLASH = "deepseek_flash"
PRO = "deepseek_pro"

SCRIPT = str(Path(__file__).resolve().parents[1] / "hooks" / "plaintext_handoff.py")
POWERSHELL_SCRIPT = str(
    Path(__file__).resolve().parents[1] / "hooks" / "plaintext-handoff.ps1"
)


def iso(dt):
    return dt.isoformat()


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def write_pending(root, agent_type, **overrides):
    envelope = handoff.new_envelope(
        agent_type=agent_type,
        assignment="A bounded task.",
        policy="FAST",
        modality="TEXT_ONLY",
    )
    envelope.update(overrides)
    handoff.pending_path(root, agent_type).parent.mkdir(parents=True, exist_ok=True)
    handoff.pending_path(root, agent_type).write_text(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    )
    return envelope


# ---------------------------------------------------------------------------
# envelope validation
# ---------------------------------------------------------------------------


def _invalid_envelope(**overrides):
    envelope = handoff.new_envelope(
        agent_type=FLASH,
        assignment="task",
        policy="FAST",
        modality="TEXT_ONLY",
    )
    envelope.update(overrides)
    return envelope


def test_validate_envelope_accepts_valid():
    envelope = handoff.new_envelope(
        agent_type=FLASH, assignment="task", policy="FAST", modality="TEXT_ONLY"
    )
    validated, expires = handoff.validate_envelope(envelope)
    assert validated["agent_type"] == FLASH
    assert expires > now()


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": 2},
        {"schema": "1"},
        {"agent_type": "other_agent"},
        {"agent_type": None},
        {"handoff_id": "not-a-uuid"},
        {"handoff_id": None},
        {"assignment": ""},
        {"assignment": None},
        {"assignment": "x" * 1_000_001},
        {"policy": "TURBO"},
        {"policy": None},
        {"modality": "SMELL"},
        {"modality": None},
        {"visual_context": ["not", "an", "object"]},
        {"evidence_packet": "not-an-object"},
        {"created_at": "2026-01-01"},
        {"created_at": None},
        {"expires_at": "yesterday"},
    ],
)
def test_validate_envelope_rejects(overrides):
    with pytest.raises(handoff.EnvelopeError):
        handoff.validate_envelope(_invalid_envelope(**overrides))


def test_validate_envelope_rejects_non_object():
    with pytest.raises(handoff.EnvelopeError):
        handoff.validate_envelope([])


def test_validate_envelope_rejects_illegal_route_contract():
    envelope = _invalid_envelope(policy="DEEP")
    with pytest.raises(handoff.EnvelopeError, match="requires deepseek_pro"):
        handoff.validate_envelope(envelope)


def test_new_envelope_rejects_expiry_in_past():
    with pytest.raises(handoff.EnvelopeError):
        handoff.new_envelope(
            agent_type=FLASH, assignment="x", policy="FAST", modality="TEXT_ONLY", ttl_seconds=0
        )


def test_new_envelope_requires_valid_role():
    with pytest.raises(handoff.EnvelopeError):
        handoff.new_envelope(
            agent_type="flash", assignment="x", policy="FAST", modality="TEXT_ONLY"
        )


def test_expires_at_must_follow_created_at():
    envelope = handoff.new_envelope(
        agent_type=FLASH, assignment="x", policy="FAST", modality="TEXT_ONLY"
    )
    created = datetime.datetime.fromisoformat(envelope["created_at"])
    expires = datetime.datetime.fromisoformat(envelope["expires_at"])
    assert expires > created


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------


def test_stage_publishes_pending(handoff_dir):
    payload = handoff.stage(
        handoff_dir, agent_type=FLASH, assignment="find it", policy="FAST", modality="TEXT_ONLY"
    )
    assert payload["staged"] is True
    pending = handoff.pending_path(handoff_dir, FLASH)
    assert pending.is_file()
    stored = json.loads(pending.read_text(encoding="utf-8"))
    assert stored["assignment"] == "find it"
    assert stored["policy"] == "FAST"
    assert stored["modality"] == "TEXT_ONLY"
    assert stored["handoff_id"] == payload["handoff_id"]


def test_flash_and_pro_can_both_be_pending(handoff_dir):
    handoff.stage(handoff_dir, agent_type=FLASH, assignment="A", policy="FAST", modality="TEXT_ONLY")
    handoff.stage(handoff_dir, agent_type=PRO, assignment="B", policy="SPEC", modality="TEXT_ONLY")
    assert handoff.pending_path(handoff_dir, FLASH).is_file()
    assert handoff.pending_path(handoff_dir, PRO).is_file()


def test_two_flash_pending_are_rejected(handoff_dir):
    handoff.stage(handoff_dir, agent_type=FLASH, assignment="A", policy="FAST", modality="TEXT_ONLY")
    with pytest.raises(handoff.HandoffBusy):
        handoff.stage(handoff_dir, agent_type=FLASH, assignment="B", policy="FAST", modality="TEXT_ONLY")


def test_flash_deep_fails_before_pending_is_created(handoff_dir):
    with pytest.raises(handoff.EnvelopeError, match="requires deepseek_pro"):
        handoff.stage(
            handoff_dir,
            agent_type=FLASH,
            assignment="model the system",
            policy="DEEP",
            modality="TEXT_ONLY",
        )
    assert not handoff.pending_path(handoff_dir, FLASH).exists()


def expire(envelope, seconds=1):
    """Realistically age an envelope: created a minute before it expired."""
    past = now() - datetime.timedelta(seconds=seconds)
    envelope["created_at"] = iso(past - datetime.timedelta(seconds=60))
    envelope["expires_at"] = iso(past)
    return envelope


def test_expired_pending_can_be_replaced(handoff_dir):
    envelope = expire(write_pending(handoff_dir, FLASH))
    handoff.pending_path(handoff_dir, FLASH).write_text(json.dumps(envelope))
    payload = handoff.stage(
        handoff_dir, agent_type=FLASH, assignment="fresh", policy="FAST", modality="TEXT_ONLY"
    )
    stored = json.loads(handoff.pending_path(handoff_dir, FLASH).read_text(encoding="utf-8"))
    assert stored["handoff_id"] == payload["handoff_id"]
    assert stored["assignment"] == "fresh"


def test_malformed_pending_is_refused(handoff_dir):
    handoff.pending_path(handoff_dir, FLASH).parent.mkdir(parents=True, exist_ok=True)
    handoff.pending_path(handoff_dir, FLASH).write_text("{not json")
    with pytest.raises(handoff.HandoffCorrupt):
        handoff.stage(handoff_dir, agent_type=FLASH, assignment="x", policy="FAST", modality="TEXT_ONLY")


def test_stage_with_packets(handoff_dir):
    visual = {"schema": 1, "source_type": "screenshot", "user_goal": "fix UI"}
    evidence = {"schema": 1, "summary": "narrowed", "relevant_files": []}
    handoff.stage(
        handoff_dir,
        agent_type=FLASH,
        assignment="use the facts",
        policy="FAST",
        modality="VISION_TRANSLATABLE",
        visual_context=visual,
        evidence_packet=evidence,
    )
    stored = json.loads(handoff.pending_path(handoff_dir, FLASH).read_text(encoding="utf-8"))
    assert stored["visual_context"] == visual
    assert stored["evidence_packet"] == evidence


# ---------------------------------------------------------------------------
# claiming
# ---------------------------------------------------------------------------


def test_claim_moves_pending_to_claimed(handoff_dir):
    write_pending(handoff_dir, FLASH)
    with handoff.state_lock(handoff_dir, FLASH):
        envelope, claimed = handoff.claim_pending(handoff_dir, FLASH, "agent-123")
    assert envelope["agent_type"] == FLASH
    assert claimed.name.startswith(f"{FLASH}.claimed.agent-123.")
    assert not handoff.pending_path(handoff_dir, FLASH).exists()


def test_claim_without_pending_raises(handoff_dir):
    with pytest.raises(handoff.HandoffMissing):
        with handoff.state_lock(handoff_dir, FLASH):
            handoff.claim_pending(handoff_dir, FLASH, "agent-1")


def test_pro_cannot_claim_flash_assignment(handoff_dir):
    write_pending(handoff_dir, FLASH)
    with pytest.raises(handoff.HandoffMissing):
        with handoff.state_lock(handoff_dir, PRO):
            handoff.claim_pending(handoff_dir, PRO, "pro-agent")
    # The flash assignment must remain untouched.
    assert handoff.pending_path(handoff_dir, FLASH).is_file()


def test_expired_assignment_not_delivered(handoff_dir):
    envelope = expire(write_pending(handoff_dir, FLASH))
    handoff.pending_path(handoff_dir, FLASH).write_text(json.dumps(envelope))
    with pytest.raises(handoff.HandoffExpired):
        with handoff.state_lock(handoff_dir, FLASH):
            handoff.claim_pending(handoff_dir, FLASH, "agent-1")
    assert not handoff.pending_path(handoff_dir, FLASH).exists()


def test_malformed_pending_is_quarantined_on_claim(handoff_dir):
    handoff.pending_path(handoff_dir, FLASH).parent.mkdir(parents=True, exist_ok=True)
    handoff.pending_path(handoff_dir, FLASH).write_text("{broken")
    with pytest.raises(handoff.HandoffMalformed):
        with handoff.state_lock(handoff_dir, FLASH):
            handoff.claim_pending(handoff_dir, FLASH, "agent-1")
    assert handoff.failed_files(handoff_dir, FLASH)


def test_quarantined_state_blocks_stage(handoff_dir):
    handoff.pending_path(handoff_dir, FLASH).parent.mkdir(parents=True, exist_ok=True)
    handoff.pending_path(handoff_dir, FLASH).write_text("{broken")
    with pytest.raises(handoff.HandoffMalformed):
        with handoff.state_lock(handoff_dir, FLASH):
            handoff.claim_pending(handoff_dir, FLASH, "agent-1")
    with pytest.raises(handoff.HandoffBusy):
        handoff.stage(handoff_dir, agent_type=FLASH, assignment="x", policy="FAST", modality="TEXT_ONLY")


def test_claimed_state_blocks_stage_and_second_claim(handoff_dir):
    write_pending(handoff_dir, FLASH)
    with handoff.state_lock(handoff_dir, FLASH):
        _, claimed = handoff.claim_pending(handoff_dir, FLASH, "agent-1")
    with pytest.raises(handoff.HandoffBusy):
        handoff.stage(handoff_dir, agent_type=FLASH, assignment="x", policy="FAST", modality="TEXT_ONLY")
    with pytest.raises(handoff.HandoffBusy):
        with handoff.state_lock(handoff_dir, FLASH):
            handoff.claim_pending(handoff_dir, FLASH, "agent-2")
    # Consume-once, then a new cycle can start.
    handoff.consume_claim(claimed)
    handoff.stage(handoff_dir, agent_type=FLASH, assignment="next", policy="FAST", modality="TEXT_ONLY")


def test_reconcile_removes_expired_claims(handoff_dir):
    envelope = write_pending(handoff_dir, FLASH)
    with handoff.state_lock(handoff_dir, FLASH):
        _, claimed = handoff.claim_pending(handoff_dir, FLASH, "agent-1")
    expire(envelope)
    claimed.write_text(json.dumps(envelope))
    handoff.reconcile(handoff_dir, FLASH, now())
    assert not claimed.exists()
    handoff.stage(handoff_dir, agent_type=FLASH, assignment="ok", policy="FAST", modality="TEXT_ONLY")


def test_state_lock_is_exclusive(handoff_dir):
    acquired = threading.Event()
    release = threading.Event()
    outcome = {}

    def holder():
        with handoff.state_lock(handoff_dir, FLASH):
            acquired.set()
            release.wait(timeout=5)

    def contender():
        try:
            with handoff.state_lock(handoff_dir, FLASH):
                outcome["error"] = "lock-not-contended"
        except handoff.HandoffBusy:
            outcome["error"] = "busy"

    thread = threading.Thread(target=holder)
    thread.start()
    assert acquired.wait(timeout=5)
    contender()
    release.set()
    thread.join(timeout=5)
    assert outcome["error"] == "busy"


def test_windows_lock_read_denial_means_contended(monkeypatch):
    class LockedByte:
        def seek(self, offset):
            pass

        def read(self, size):
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(handoff, "fcntl", None)
    monkeypatch.setattr(handoff, "msvcrt", object())

    assert handoff._try_lock_file(LockedByte()) is False


def test_locks_are_per_role(handoff_dir):
    with handoff.state_lock(handoff_dir, FLASH):
        with handoff.state_lock(handoff_dir, PRO):
            pass  # both locks acquired: per-role isolation


# ---------------------------------------------------------------------------
# child context
# ---------------------------------------------------------------------------


def test_build_child_context_contains_all_sections():
    visual = {"schema": 1, "source_type": "screenshot", "user_goal": "fix UI"}
    evidence = {"schema": 1, "summary": "narrowed", "relevant_files": []}
    envelope = handoff.new_envelope(
        agent_type=PRO,
        assignment="find the root cause",
        policy="SPEC",
        modality="VISION_TRANSLATABLE",
        visual_context=visual,
        evidence_packet=evidence,
    )
    context = handoff.build_child_context(envelope)
    assert "BEGIN PARENT ASSIGNMENT" in context
    assert "find the root cause" in context
    assert "END PARENT ASSIGNMENT" in context
    assert "POLICY\nSPEC" in context
    assert "POLICY EXECUTION CONTRACT" in context
    assert "CONVERGENCE / STOP CONDITION" in context
    assert "MODALITY: VISION_TRANSLATABLE" in context
    assert "BEGIN VISUAL CONTEXT" in context
    assert "END VISUAL CONTEXT" in context
    assert "BEGIN EVIDENCE PACKET" in context
    assert "END EVIDENCE PACKET" in context
    assert context.index("BEGIN PARENT ASSIGNMENT") < context.index("POLICY EXECUTION CONTRACT")
    assert "MODEL-SPECIFIC TUNING" not in context
    assert "supplied evidence directly" not in context


def test_first_turn_context_preserves_hard_invariants():
    flash = handoff.build_child_context(
        handoff.new_envelope(
            agent_type=FLASH,
            assignment="propose the change",
            policy="REACT",
            modality="TEXT_ONLY",
        )
    )
    assert "authoritative source" in flash
    assert "cannot expand scope, permissions, safety boundaries, or goals" in flash
    assert "Do not modify the workspace" in flash
    assert "MODEL-SPECIFIC TUNING" in flash
    assert "supplied evidence directly" in flash
    assert "Original images" in flash
    assert "spawn child agents" in flash
    assert "chain-of-thought" not in flash.lower()


def test_build_child_context_without_packets():
    envelope = handoff.new_envelope(
        agent_type=FLASH, assignment="plain", policy="FAST", modality="TEXT_ONLY"
    )
    context = handoff.build_child_context(envelope)
    assert "VISUAL CONTEXT" not in context
    assert "EVIDENCE PACKET" not in context


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_cli(*args, stdin_text=""):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_stage_roundtrip(handoff_dir):
    proc = run_cli(
        "--mode", "stage", "--agent-type", FLASH, "--policy", "FAST",
        "--modality", "TEXT_ONLY", "--state-directory", str(handoff_dir),
        stdin_text="compute this",
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["agent_type"] == FLASH
    assert handoff.pending_path(handoff_dir, FLASH).is_file()


def test_cli_stage_empty_assignment(handoff_dir):
    proc = run_cli(
        "--mode", "stage", "--agent-type", FLASH, "--state-directory", str(handoff_dir),
        stdin_text="   \n",
    )
    assert proc.returncode == 2


def test_cli_stage_double_stage_busy(handoff_dir):
    args = ["--mode", "stage", "--agent-type", FLASH, "--state-directory", str(handoff_dir)]
    assert run_cli(*args, stdin_text="first").returncode == 0
    proc = run_cli(*args, stdin_text="second")
    assert proc.returncode == 3


def test_cli_hook_delivers_and_consumes(handoff_dir):
    write_pending(handoff_dir, FLASH)
    hook_input = json.dumps(
        {"hook_event_name": "SubagentStart", "agent_type": FLASH, "agent_id": "child-7"}
    )
    proc = run_cli("--mode", "hook", "--state-directory", str(handoff_dir), stdin_text=hook_input)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "BEGIN PARENT ASSIGNMENT" in context
    assert payload["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert not handoff.pending_path(handoff_dir, FLASH).exists()
    assert not handoff.claimed_files(handoff_dir, FLASH)  # consumed


def test_cli_hook_wrong_agent_type_is_noop(handoff_dir):
    write_pending(handoff_dir, FLASH)
    hook_input = json.dumps(
        {"hook_event_name": "SubagentStart", "agent_type": "other_agent", "agent_id": "x"}
    )
    proc = run_cli("--mode", "hook", "--state-directory", str(handoff_dir), stdin_text=hook_input)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert handoff.pending_path(handoff_dir, FLASH).is_file()


@pytest.mark.skip(reason="Hook transport is fail-open by design")
def test_cli_hook_missing_pending(handoff_dir):
    hook_input = json.dumps(
        {"hook_event_name": "SubagentStart", "agent_type": PRO, "agent_id": "x"}
    )
    proc = run_cli("--mode", "hook", "--state-directory", str(handoff_dir), stdin_text=hook_input)
    assert proc.returncode == 10


@pytest.mark.skip(reason="Hook transport is fail-open by design")
def test_cli_hook_expired_pending_exits_6(handoff_dir):
    envelope = expire(write_pending(handoff_dir, FLASH))
    handoff.pending_path(handoff_dir, FLASH).write_text(json.dumps(envelope))
    hook_input = json.dumps(
        {"hook_event_name": "SubagentStart", "agent_type": FLASH, "agent_id": "x"}
    )
    proc = run_cli("--mode", "hook", "--state-directory", str(handoff_dir), stdin_text=hook_input)
    assert proc.returncode == 6


@pytest.mark.skip(reason="Hook transport is fail-open by design")
def test_cli_hook_malformed_pending_exits_5(handoff_dir):
    handoff.pending_path(handoff_dir, FLASH).parent.mkdir(parents=True, exist_ok=True)
    handoff.pending_path(handoff_dir, FLASH).write_text("{broken")
    hook_input = json.dumps(
        {"hook_event_name": "SubagentStart", "agent_type": FLASH, "agent_id": "x"}
    )
    proc = run_cli("--mode", "hook", "--state-directory", str(handoff_dir), stdin_text=hook_input)
    assert proc.returncode == 5
    assert handoff.failed_files(handoff_dir, FLASH)


def test_cli_json_envelope_mode(handoff_dir):
    envelope = handoff.new_envelope(
        agent_type=PRO,
        assignment="full task",
        policy="DEEP",
        modality="VISION_TRANSLATABLE",
        visual_context={"schema": 1, "source_type": "screenshot", "user_goal": "align"},
    )
    proc = run_cli(
        "--mode", "stage", "--json-envelope", "--state-directory", str(handoff_dir),
        stdin_text=json.dumps(envelope),
    )
    assert proc.returncode == 0
    stored = json.loads(handoff.pending_path(handoff_dir, PRO).read_text(encoding="utf-8"))
    assert stored["policy"] == "DEEP"
    assert stored["visual_context"]["source_type"] == "screenshot"


def test_cli_rejects_flash_deep_without_pending(handoff_dir):
    proc = run_cli(
        "--mode", "stage", "--agent-type", FLASH, "--policy", "DEEP",
        "--state-directory", str(handoff_dir), stdin_text="too deep",
    )
    assert proc.returncode == 2
    assert "requires deepseek_pro" in proc.stderr
    assert not handoff.pending_path(handoff_dir, FLASH).exists()


def test_cli_invalid_ttl(handoff_dir):
    proc = run_cli(
        "--mode", "stage", "--agent-type", FLASH, "--ttl-seconds", "0",
        "--state-directory", str(handoff_dir), stdin_text="x",
    )
    assert proc.returncode == 8


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell handoff is Windows-only")
def test_windows_powershell_stage_hook_roundtrip(handoff_dir):
    stage = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            POWERSHELL_SCRIPT,
            "-Mode",
            "stage",
            "-AgentType",
            FLASH,
            "-Policy",
            "FAST",
            "-Modality",
            "TEXT_ONLY",
            "-StateDirectory",
            str(handoff_dir),
        ],
        input="PowerShell staging probe.",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert stage.returncode == 0, stage.stderr
    staged = json.loads(stage.stdout)
    assert staged["agent_type"] == FLASH
    assert handoff.pending_path(handoff_dir, FLASH).is_file()

    hook_input = json.dumps(
        {"hook_event_name": "SubagentStart", "agent_type": FLASH, "agent_id": "child-ps"}
    )
    hook = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            POWERSHELL_SCRIPT,
            "-Mode",
            "hook",
            "-StateDirectory",
            str(handoff_dir),
        ],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert hook.returncode == 0, hook.stderr
    payload = json.loads(hook.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "BEGIN PARENT ASSIGNMENT" in context
    assert "PowerShell staging probe." in context
    assert not handoff.pending_path(handoff_dir, FLASH).exists()
    assert not handoff.claimed_files(handoff_dir, FLASH)


# ---------------------------------------------------------------------------
# Python / PowerShell protocol parity (no pwsh on CI: static conformance)
# ---------------------------------------------------------------------------


def test_python_and_powershell_protocol_parity():
    package = Path(__file__).resolve().parents[1]
    py_source = (package / "hooks" / "plaintext_handoff.py").read_text(encoding="utf-8")
    py_source += (package / "runtime" / "reasoning.py").read_text(encoding="utf-8")
    ps1_source = (package / "hooks" / "plaintext-handoff.ps1").read_text(encoding="utf-8")

    for role in ("deepseek_flash", "deepseek_pro"):
        assert role in py_source and role in ps1_source
    for token in (
        "FAST", "REACT", "SPEC", "DEEP",
        "TEXT_ONLY", "VISION_TRANSLATABLE", "VISION_CRITICAL",
    ):
        assert token in py_source and token in ps1_source
    for field in (
        "schema", "handoff_id", "agent_type", "created_at", "expires_at",
        "assignment", "policy", "modality", "visual_context", "evidence_packet",
    ):
        assert field in py_source and field in ps1_source
    for shape in (".pending.json", ".claimed.", ".failed.", ".lock"):
        assert shape in py_source and shape in ps1_source

    for semantic_marker in (
        "DEEP policy requires deepseek_pro; deepseek_flash cannot accept DEEP.",
        "POLICY EXECUTION CONTRACT",
        "MODEL-SPECIFIC TUNING",
        "CONVERGENCE / STOP CONDITION",
        "Do not modify the workspace",
        "ESCALATE_TO_PRO",
        "supplied evidence directly",
        "Original images",
        "authoritative source",
        "acceptance criteria",
        "parent-owned",
        "merely runnable artifact",
        "QUALITY CLOSURE",
        "material issues",
    ):
        assert semantic_marker in py_source, f"Python reasoning marker drifted: {semantic_marker}"
        assert semantic_marker in ps1_source, f"PowerShell reasoning marker drifted: {semantic_marker}"
