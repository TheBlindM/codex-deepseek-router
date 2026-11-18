#!/usr/bin/env python3
"""Install, verify and manage the Codex -> DeepSeek dual-agent router.

Adapted from oil-oil/codex-deepseek-subagent (MIT), scripts/codex_deepseek.py.
See docs/upstream-reference-map.md for the per-symbol source map.

The router installs two native Codex child agents side by side:

    deepseek_flash -> deepseek-v4-flash   (fast bounded worker)
    deepseek_pro   -> deepseek-v4-pro     (deep solver / reviewer)

The Codex parent model and provider are never changed. The DeepSeek provider
is declared inside each agent TOML (env_key on Windows/Linux, macOS Keychain
command auth on macOS), and cross-provider task delivery uses the plaintext
SubagentStart handoff transport (hooks/plaintext_handoff.py).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PACKAGE_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

from runtime.provider_config import (  # noqa: E402 - standalone manager bootstraps package root
    DEFAULT_BASE_URL,
    DEFAULT_FLASH_MODEL,
    DEFAULT_PRO_MODEL,
    ProviderConfigError,
    RouterConfig,
    load_router_config,
    settings_payload,
)
from runtime.reasoning import (  # noqa: E402 - standalone manager bootstraps package root
    RouteContractError,
    render_reasoning_context,
)

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # macOS / Linux
    msvcrt = None

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - depends on interpreter version
    tomllib = None

try:
    import tomli  # Optional backport
except ImportError:
    tomli = None

try:
    from enum import StrEnum  # Python 3.11+
except ImportError:  # Python < 3.11 - str-mixin shim with identical behavior
    from enum import Enum as _Enum

    class StrEnum(str, _Enum):
        def __str__(self) -> str:
            return str(self.value)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_NAME = "codex-deepseek-router"
FLASH_ROLE = "deepseek_flash"
PRO_ROLE = "deepseek_pro"
PROVIDER = "deepseek"
WIRE_API = "responses"
# Backward-compatible names for callers that only need the built-in defaults.
FLASH_MODEL = DEFAULT_FLASH_MODEL
PRO_MODEL = DEFAULT_PRO_MODEL
BASE_URL = DEFAULT_BASE_URL
HASH_VERSION_EXACT_BYTES = 2
API_KEY_ENV = "DEEPSEEK_API_KEY"

SUPPORTED_ROLES = {
    FLASH_ROLE: FLASH_MODEL,
    PRO_ROLE: PRO_MODEL,
}
VALID_AGENTS = set(SUPPORTED_ROLES)
POLICIES = ("FAST", "REACT", "SPEC", "DEEP")

CREDENTIAL_TARGET = "io.github.codex-deepseek-router.deepseek-api-key"
HOOKS_INSTALL_DIR_NAME = PROJECT_NAME
MAX_STATE_DATABASES = 32
METADATA_WAIT_SECONDS = 5.0
LOCK_WAIT_SECONDS = 5.0
APP_SERVER_TIMEOUT_SECONDS = 10.0
DIRECT_CODEX_TIMEOUT_SECONDS = 900
COMPLEX_PRO_TIMEOUT_SECONDS = 1800
MAX_ASSIGNMENT_CHARS = 1_000_000

DESKTOP_CODEX_CANDIDATES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)
WINDOWS_CODEX_RELATIVE_CANDIDATES = (
    Path("Programs") / "Codex" / "resources" / "codex.exe",
    Path("Programs") / "OpenAI" / "Codex" / "resources" / "codex.exe",
    Path("Codex") / "resources" / "codex.exe",
)


class Modality(StrEnum):
    TEXT_ONLY = "TEXT_ONLY"
    VISION_TRANSLATABLE = "VISION_TRANSLATABLE"
    VISION_CRITICAL = "VISION_CRITICAL"


class TransportMode(StrEnum):
    NATIVE = "native"
    DIRECT_CODEX = "direct_codex"
    PLAINTEXT_HOOK = "plaintext_hook"
    LEGACY_V1 = "legacy_v1"


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def deepseek_allowed(modality: Optional[str]) -> bool:
    return modality in {
        Modality.TEXT_ONLY,
        Modality.VISION_TRANSLATABLE,
    }


def codex_requires_direct_transport(version_text: Optional[str]) -> bool:
    """Whether this Codex version blocks cross-provider role overrides."""
    if not version_text:
        return False
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)", version_text)
    if not match:
        return False
    major, minor, _patch = (int(part) for part in match.groups())
    return (major, minor) >= (0, 149)


def choose_transport(
    native_probe_ok: bool,
    hook_available: bool,
    codex_version: Optional[str] = None,
) -> TransportMode:
    if codex_requires_direct_transport(codex_version):
        return TransportMode.DIRECT_CODEX
    if native_probe_ok:
        return TransportMode.NATIVE
    if hook_available:
        return TransportMode.PLAINTEXT_HOOK
    return TransportMode.LEGACY_V1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManagerError(RuntimeError):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class HandoffBusy(RuntimeError):
    """A handoff slot for this agent role is already occupied."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    codex_home: Path

    @property
    def config(self) -> Path:
        return self.codex_home / "config.toml"

    @property
    def catalog(self) -> Path:
        return self.codex_home / "models.json"

    @property
    def agents_dir(self) -> Path:
        return self.codex_home / "agents"

    @property
    def flash_agent(self) -> Path:
        return self.agents_dir / "deepseek-flash.toml"

    @property
    def pro_agent(self) -> Path:
        return self.agents_dir / "deepseek-pro.toml"

    @property
    def state_dir(self) -> Path:
        return self.codex_home / "deepseek-router"

    @property
    def manifest(self) -> Path:
        return self.state_dir / "manifest.json"

    @property
    def settings(self) -> Path:
        return self.state_dir / "settings.json"

    @property
    def handoff_dir(self) -> Path:
        return self.state_dir / "handoff"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def hooks_config(self) -> Path:
        return self.codex_home / "hooks.json"

    @property
    def hooks_install_dir(self) -> Path:
        return self.codex_home / "hooks" / HOOKS_INSTALL_DIR_NAME

    @property
    def plugin_root(self) -> Path:
        return package_root()

    @property
    def plugin_manifest(self) -> Path:
        return self.plugin_root / ".codex-plugin" / "plugin.json"

    @property
    def plugin_hooks_config(self) -> Path:
        return self.plugin_root / "hooks" / "hooks.json"

    def agent_path(self, agent_type: str) -> Path:
        if agent_type == FLASH_ROLE:
            return self.flash_agent
        if agent_type == PRO_ROLE:
            return self.pro_agent
        raise ValueError(f"unknown agent type: {agent_type}")


@dataclass(frozen=True)
class AgentSpec:
    role: str
    model: str
    description: str
    sandbox_mode: str


AGENT_SPECS = {
    FLASH_ROLE: AgentSpec(
        role=FLASH_ROLE,
        model=FLASH_MODEL,
        description=(
            "Fast text-only read-only DeepSeek worker for bounded exploration, "
            "search, logs, extraction and pre-implementation analysis."
        ),
        sandbox_mode="read-only",
    ),
    PRO_ROLE: AgentSpec(
        role=PRO_ROLE,
        model=PRO_MODEL,
        description=(
            "Deep text-only DeepSeek solver for root cause, architecture, "
            "concurrency, review and difficult implementation."
        ),
        sandbox_mode="workspace-write",
    ),
}


@dataclass(frozen=True)
class RoutingDecision:
    agent: str  # NONE | FLASH | PRO
    policy: str  # FAST | REACT | SPEC | DEEP
    modality: str
    reason: str


@dataclass
class VisualContext:
    source_type: str
    user_goal: str
    observations: List[str] = field(default_factory=list)
    visible_text: List[str] = field(default_factory=list)
    relationships: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": 1,
            "source_type": self.source_type,
            "user_goal": self.user_goal,
            "observations": self.observations,
            "visible_text": self.visible_text,
            "relationships": self.relationships,
            "uncertainties": self.uncertainties,
            "source_visibility": "parent_only",
        }


@dataclass
class EvidencePacket:
    summary: str
    relevant_files: List[str]
    observations: List[str]
    hypotheses: List[str]
    eliminated: List[str]
    open_questions: List[str]
    recommended_next_step: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": 1,
            "summary": self.summary,
            "relevant_files": self.relevant_files,
            "observations": self.observations,
            "hypotheses": self.hypotheses,
            "eliminated": self.eliminated,
            "open_questions": self.open_questions,
            "recommended_next_step": self.recommended_next_step,
        }


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def result(status: str, **kwargs: Any) -> Dict[str, Any]:
    return {"status": status, **kwargs}


def emit(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(payload.get("status", "unknown"))
    for key, value in payload.items():
        if key != "status":
            print(f"{key}: {value}")


def manager_error_result(exc: ManagerError) -> Dict[str, Any]:
    """Serialize manager errors without allowing detail keys to replace the code."""
    details = dict(exc.details)
    if "status" in details:
        details["configuration_status"] = details.pop("status")
    if "message" in details:
        details["detail_message"] = details.pop("message")
    return result(exc.code, message=str(exc), **details)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text_file(path: Path) -> str:
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return sha256_bytes(normalized.encode())


def sha256_file(path: Path) -> str:
    """Hash exact bytes for ownership and transaction integrity checks."""
    return sha256_bytes(path.read_bytes())


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def resolve_paths(codex_home: Optional[str]) -> Paths:
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return Paths(home.expanduser().resolve())


def package_root() -> Path:
    """The installed package dir (the one containing scripts/, agents/, skills/)."""
    return Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    """The repository checkout root when running from a source checkout."""
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Minimal TOML access (stdlib-only, Python 3.9 compatible)
# ---------------------------------------------------------------------------


def load_toml(text: str) -> Dict[str, Any]:
    if tomllib is not None:
        return tomllib.loads(text)
    if tomli is not None:
        return tomli.loads(text)
    raise ManagerError(
        "toml_parser_missing",
        "No TOML parser is available in this Python runtime (3.11+ or tomli required).",
    )


def toml_top_level_string(text: str, key: str) -> Optional[str]:
    """Best-effort top-level `key = "value"` extraction without a full parser.

    Used only for reading the parent model/provider from config.toml on
    Python runtimes that have neither tomllib nor tomli. The regex only
    matches simple single-line string assignments before the first table
    header, which is exactly the shape Codex writes for these keys.
    """
    pattern = re.compile(
        r'^\s*' + re.escape(key) + r'\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$'
    )
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            break
        match = pattern.match(line)
        if match:
            return match.group(1).encode().decode("unicode_escape")
    return None


def toml_get_top_level_string(text: str, key: str) -> Optional[str]:
    if not text.strip():
        return None
    if tomllib is not None or tomli is not None:
        try:
            value = load_toml(text).get(key)
            return value if isinstance(value, str) else None
        except ManagerError:
            return None
    return toml_top_level_string(text, key)


def toml_has_table(text: str, table: str) -> bool:
    """True when a `[table]` section (or a dotted sub-table) exists."""
    tokens = [
        r'(?:' + re.escape(part) + r'|"' + re.escape(part) + r'"|' + re.escape("'" + part + "'") + r')'
        for part in table.split(".")
    ]
    pattern = re.compile(r"^\[\s*" + r"\s*\.\s*".join(tokens) + r"\s*(\]|\.)")
    return any(pattern.match(line.strip()) for line in text.splitlines())


# ---------------------------------------------------------------------------
# Codex runtime discovery (ported from oil-oil)
# ---------------------------------------------------------------------------


def find_desktop_codex() -> str:
    configured = os.environ.get("CODEX_DESKTOP_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise ManagerError(
            "desktop_codex_missing",
            f"CODEX_DESKTOP_BIN points to a missing file: {candidate}",
        )

    candidates: List[Path] = []
    platform = platform_name()
    if platform == "macos":
        candidates.extend(DESKTOP_CODEX_CANDIDATES)
    elif platform == "windows":
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            root = os.environ.get(variable)
            if root:
                candidates.extend(
                    Path(root) / relative for relative in WINDOWS_CODEX_RELATIVE_CANDIDATES
                )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    discovered = shutil.which("codex.exe") if platform == "windows" else shutil.which("codex")
    if discovered:
        return discovered

    raise ManagerError(
        "desktop_codex_missing",
        "Could not find the Codex runtime. Install or start the Codex desktop app, "
        "or set CODEX_DESKTOP_BIN to the bundled codex binary.",
    )


def codex_version_text(codex_bin: str) -> str:
    proc = subprocess.run(
        [codex_bin, "--version"], capture_output=True, text=True, timeout=15
    )
    text = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0 or not text:
        raise ManagerError("codex_version_unknown", "Could not read the Codex runtime version.")
    return text


# ---------------------------------------------------------------------------
# Credentials (ported from oil-oil, retargeted credential name)
# ---------------------------------------------------------------------------


def credential_account() -> str:
    return getpass.getuser()


def credential_backend() -> Optional[str]:
    platform = platform_name()
    if platform == "macos":
        return "macos-keychain"
    if platform == "windows":
        return "windows-credential-manager"
    return None


def credential_present() -> bool:
    if os.environ.get(API_KEY_ENV):
        return True
    backend = credential_backend()
    try:
        if backend == "macos-keychain":
            return _macos_credential_exists()
        if backend == "windows-credential-manager":
            return _windows_read_credential() is not None
        # Linux V1: environment variable only.
        return False
    except ManagerError:
        return False


def _macos_read_credential() -> Optional[str]:
    value = _macos_copy_credential(return_data=True)
    return value if isinstance(value, str) else None


def _macos_credential_exists() -> bool:
    """Check for the item without asking Keychain to decrypt its value."""
    return _macos_copy_credential(return_data=False) is True


_K_CF_STRING_ENCODING_UTF8 = 0x08000100
_ERR_SEC_SUCCESS = 0
_ERR_SEC_DUPLICATE_ITEM = -25299
_ERR_SEC_ITEM_NOT_FOUND = -25300


def _macos_security_framework():
    """Load Security.framework / CoreFoundation via ctypes.

    Used for the credential write path so the secret never enters argv (the
    `security` CLI's `-w` option would, and Apple marks that usage unsafe).
    """
    import ctypes
    from ctypes import c_void_p

    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

    security.SecItemAdd.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]
    security.SecItemAdd.restype = ctypes.c_int32
    security.SecItemCopyMatching.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]
    security.SecItemCopyMatching.restype = ctypes.c_int32
    security.SecItemUpdate.argtypes = [c_void_p, c_void_p]
    security.SecItemUpdate.restype = ctypes.c_int32
    security.SecItemDelete.argtypes = [c_void_p]
    security.SecItemDelete.restype = ctypes.c_int32

    core_foundation.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_int32]
    core_foundation.CFStringCreateWithCString.restype = c_void_p
    core_foundation.CFDataCreate.argtypes = [c_void_p, c_void_p, ctypes.c_long]
    core_foundation.CFDataCreate.restype = c_void_p
    core_foundation.CFDictionaryCreate.argtypes = [
        c_void_p,
        ctypes.POINTER(c_void_p),
        ctypes.POINTER(c_void_p),
        ctypes.c_long,
        c_void_p,
        c_void_p,
    ]
    core_foundation.CFDictionaryCreate.restype = c_void_p
    core_foundation.CFDataGetLength.argtypes = [c_void_p]
    core_foundation.CFDataGetLength.restype = ctypes.c_long
    core_foundation.CFDataGetBytePtr.argtypes = [c_void_p]
    core_foundation.CFDataGetBytePtr.restype = c_void_p
    core_foundation.CFRelease.argtypes = [c_void_p]
    core_foundation.CFRelease.restype = None
    return security, core_foundation, ctypes


def _macos_security_constants(security, core_foundation, ctypes) -> Dict[str, int]:
    """Resolve Security/CoreFoundation constants as their exported pointers."""

    def exported_pointer(library, name: str) -> int:
        value = ctypes.c_void_p.in_dll(library, name).value
        if value is None:
            raise ValueError(f"macOS framework symbol {name} was null")
        return value

    def exported_struct(library, name: str) -> int:
        return ctypes.addressof(ctypes.c_byte.in_dll(library, name))

    return {
        "class": exported_pointer(security, "kSecClass"),
        "generic_password": exported_pointer(security, "kSecClassGenericPassword"),
        "service": exported_pointer(security, "kSecAttrService"),
        "account": exported_pointer(security, "kSecAttrAccount"),
        "value_data": exported_pointer(security, "kSecValueData"),
        "return_data": exported_pointer(security, "kSecReturnData"),
        "true": exported_pointer(core_foundation, "kCFBooleanTrue"),
        "key_callbacks": exported_struct(core_foundation, "kCFTypeDictionaryKeyCallBacks"),
        "value_callbacks": exported_struct(core_foundation, "kCFTypeDictionaryValueCallBacks"),
    }


def _macos_copy_credential(return_data: bool):
    """Query Keychain through the same Python identity that creates the item."""
    try:
        security, cf, ctypes = _macos_security_framework()
        constants = _macos_security_constants(security, cf, ctypes)
    except (OSError, ValueError) as exc:
        raise ManagerError(
            "credential_read_failed",
            f"Could not load Security.framework: {exc}",
        ) from exc

    def cf_string(value: str):
        return cf.CFStringCreateWithCString(
            None, value.encode("utf-8"), _K_CF_STRING_ENCODING_UTF8
        )

    def dictionary(entries):
        keys = (ctypes.c_void_p * len(entries))(*[key for key, _ in entries])
        values = (ctypes.c_void_p * len(entries))(*[value for _, value in entries])
        return cf.CFDictionaryCreate(
            None,
            keys,
            values,
            len(entries),
            constants["key_callbacks"],
            constants["value_callbacks"],
        )

    with ExitStack() as cleanup:
        def owned(ref, label: str):
            if not ref:
                raise ManagerError(
                    "credential_read_failed",
                    f"CoreFoundation could not allocate {label}.",
                )
            cleanup.callback(cf.CFRelease, ref)
            return ref

        target_ref = owned(cf_string(CREDENTIAL_TARGET), "the Keychain service string")
        user_ref = owned(cf_string(credential_account()), "the Keychain account string")
        entries = [
            (constants["class"], constants["generic_password"]),
            (constants["service"], target_ref),
            (constants["account"], user_ref),
        ]
        if return_data:
            entries.append((constants["return_data"], constants["true"]))
        query = owned(dictionary(entries), "the Keychain query")
        result_ref = ctypes.c_void_p()
        status = security.SecItemCopyMatching(
            query,
            ctypes.byref(result_ref) if return_data else None,
        )
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return None if return_data else False
        if status != _ERR_SEC_SUCCESS:
            raise ManagerError(
                "credential_read_failed",
                f"Keychain credential query failed (status {status}).",
            )
        if not return_data:
            return True
        data_ref = owned(result_ref.value, "the Keychain credential data")
        length = cf.CFDataGetLength(data_ref)
        pointer = cf.CFDataGetBytePtr(data_ref)
        if length < 0 or (length and not pointer):
            raise ManagerError(
                "credential_read_failed",
                "Keychain returned invalid credential data.",
            )
        try:
            return ctypes.string_at(pointer, length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManagerError(
                "credential_read_failed",
                "Keychain credential data was not valid UTF-8.",
            ) from exc


def _macos_store_credential(secret: str) -> None:
    """Store the API key through SecItemAdd. The secret never enters argv."""
    try:
        security, cf, ctypes = _macos_security_framework()
        constants = _macos_security_constants(security, cf, ctypes)
    except (OSError, ValueError) as exc:
        raise ManagerError(
            "credential_write_failed",
            f"Could not load Security.framework: {exc}",
        ) from exc

    def cf_string(value: str):
        return cf.CFStringCreateWithCString(None, value.encode("utf-8"), _K_CF_STRING_ENCODING_UTF8)

    def cf_data(value: bytes):
        buffer = ctypes.create_string_buffer(value)
        return cf.CFDataCreate(None, buffer, len(value))

    def dictionary(entries):
        keys = (ctypes.c_void_p * len(entries))(*[key for key, _ in entries])
        values = (ctypes.c_void_p * len(entries))(*[value for _, value in entries])
        return cf.CFDictionaryCreate(
            None,
            keys,
            values,
            len(entries),
            constants["key_callbacks"],
            constants["value_callbacks"],
        )

    with ExitStack() as cleanup:
        def owned(ref, label: str):
            if not ref:
                raise ManagerError(
                    "credential_write_failed",
                    f"CoreFoundation could not allocate {label}.",
                )
            cleanup.callback(cf.CFRelease, ref)
            return ref

        target_ref = owned(cf_string(CREDENTIAL_TARGET), "the Keychain service string")
        user_ref = owned(cf_string(credential_account()), "the Keychain account string")
        secret_ref = owned(cf_data(secret.encode("utf-8")), "the credential data")
        query = owned(
            dictionary(
                [
                    (constants["class"], constants["generic_password"]),
                    (constants["service"], target_ref),
                    (constants["account"], user_ref),
                ]
            ),
            "the Keychain query",
        )
        item = owned(
            dictionary(
                [
                    (constants["class"], constants["generic_password"]),
                    (constants["service"], target_ref),
                    (constants["account"], user_ref),
                    (constants["value_data"], secret_ref),
                ]
            ),
            "the Keychain item",
        )
        status = security.SecItemAdd(item, None)
        if status == _ERR_SEC_DUPLICATE_ITEM:
            updates = owned(
                dictionary([(constants["value_data"], secret_ref)]),
                "the Keychain update",
            )
            status = security.SecItemUpdate(query, updates)
        if status != _ERR_SEC_SUCCESS:
            raise ManagerError(
                "credential_write_failed",
                f"Keychain credential write failed (status {status}).",
            )


def _macos_remove_credential() -> bool:
    try:
        security, cf, ctypes = _macos_security_framework()
        constants = _macos_security_constants(security, cf, ctypes)
    except (OSError, ValueError) as exc:
        raise ManagerError(
            "credential_delete_failed",
            f"Could not load Security.framework: {exc}",
        ) from exc

    def cf_string(value: str):
        return cf.CFStringCreateWithCString(
            None, value.encode("utf-8"), _K_CF_STRING_ENCODING_UTF8
        )

    with ExitStack() as cleanup:
        target_ref = cf_string(CREDENTIAL_TARGET)
        user_ref = cf_string(credential_account())
        if not target_ref or not user_ref:
            if target_ref:
                cf.CFRelease(target_ref)
            if user_ref:
                cf.CFRelease(user_ref)
            raise ManagerError(
                "credential_delete_failed",
                "CoreFoundation could not allocate the Keychain query strings.",
            )
        cleanup.callback(cf.CFRelease, target_ref)
        cleanup.callback(cf.CFRelease, user_ref)
        entries = [
            (constants["class"], constants["generic_password"]),
            (constants["service"], target_ref),
            (constants["account"], user_ref),
        ]
        keys = (ctypes.c_void_p * len(entries))(*[key for key, _ in entries])
        values = (ctypes.c_void_p * len(entries))(*[value for _, value in entries])
        query = cf.CFDictionaryCreate(
            None,
            keys,
            values,
            len(entries),
            constants["key_callbacks"],
            constants["value_callbacks"],
        )
        if not query:
            raise ManagerError(
                "credential_delete_failed",
                "CoreFoundation could not allocate the Keychain delete query.",
            )
        cleanup.callback(cf.CFRelease, query)
        status = security.SecItemDelete(query)
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return False
        if status != _ERR_SEC_SUCCESS:
            raise ManagerError(
                "credential_delete_failed",
                f"Keychain credential delete failed (status {status}).",
            )
        return True


def _windows_credential_api():
    import ctypes
    from ctypes import wintypes

    class CredentialW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CredentialW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CredentialW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None
    return ctypes, CredentialW, advapi32


def _windows_read_credential() -> Optional[str]:
    ctypes, credential_type, advapi32 = _windows_credential_api()
    credential = ctypes.POINTER(credential_type)()
    if not advapi32.CredReadW(CREDENTIAL_TARGET, 1, 0, ctypes.byref(credential)):
        error = ctypes.get_last_error()
        if error == 1168:
            return None
        raise ManagerError(
            "credential_read_failed",
            f"Could not read Windows Credential Manager (error {error}).",
        )
    try:
        raw = ctypes.string_at(
            credential.contents.CredentialBlob,
            credential.contents.CredentialBlobSize,
        )
        return raw.decode("utf-8")
    finally:
        advapi32.CredFree(credential)


def _windows_store_credential(secret: str) -> None:
    ctypes, credential_type, advapi32 = _windows_credential_api()
    raw = secret.encode("utf-8")
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = credential_type()
    credential.Flags = 0
    credential.Type = 1
    credential.TargetName = CREDENTIAL_TARGET
    credential.CredentialBlobSize = len(raw)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2
    credential.UserName = credential_account()
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise ManagerError(
            "credential_write_failed",
            f"Could not write the API key to Windows Credential Manager (error {error}).",
        )


def _windows_remove_credential() -> bool:
    ctypes, _, advapi32 = _windows_credential_api()
    if advapi32.CredDeleteW(CREDENTIAL_TARGET, 1, 0):
        return True
    error = ctypes.get_last_error()
    if error == 1168:
        return False
    raise ManagerError(
        "credential_delete_failed",
        f"Could not remove the API key from Windows Credential Manager (error {error}).",
    )


def read_credential_key() -> Optional[str]:
    backend = credential_backend()
    if backend == "macos-keychain":
        stored = _macos_read_credential()
        return stored if stored is not None else os.environ.get(API_KEY_ENV)
    if backend == "windows-credential-manager":
        stored = _windows_read_credential()
        return stored if stored is not None else os.environ.get(API_KEY_ENV)
    return os.environ.get(API_KEY_ENV)


def store_api_key(secret: str) -> None:
    if not secret.startswith("sk-"):
        raise ManagerError("invalid_api_key", "The DeepSeek API key must start with sk-.")
    backend = credential_backend()
    if backend == "macos-keychain":
        _macos_store_credential(secret)
        return
    if backend == "windows-credential-manager":
        _windows_store_credential(secret)
        return
    raise ManagerError(
        "unsupported_platform",
        "This platform has no system credential store. Set DEEPSEEK_API_KEY in the "
        "environment instead (Linux V1 behavior).",
    )


def remove_api_key() -> bool:
    if not credential_present():
        return False
    backend = credential_backend()
    if backend == "macos-keychain":
        return _macos_remove_credential()
    if backend == "windows-credential-manager":
        return _windows_remove_credential()
    return False


# ---------------------------------------------------------------------------
# Agent TOML templates
# ---------------------------------------------------------------------------

_FLASH_INSTRUCTIONS = """\
You are a bounded DeepSeek Flash child agent.

Follow the parent assignment exactly.
Answer in the same language as the parent assignment unless it explicitly
requires another language.

Do not broaden scope.
Do not spawn additional agents.
Do not claim to see images or screenshots.

You are read-only. Never modify workspace files: return findings, analysis,
or a proposed change as text so the parent can land the edit.

If VISUAL_CONTEXT is supplied, treat it only as parent-provided facts.

If you return ESCALATE_TO_PRO, include an EVIDENCE_PACKET with summary,
relevant_files, observations, hypotheses, eliminated, open_questions, and
recommended_next_step. Observations must be reproducible facts.

If a BEGIN PARENT ASSIGNMENT / END PARENT ASSIGNMENT block exists,
treat it as the authoritative task contract. Dynamic Policy Execution
Contract, Model-specific Tuning (when present), and Stop Condition blocks
control how to execute that assignment without changing its scope or permissions.
"""

_PRO_INSTRUCTIONS = """\
You are a DeepSeek Pro child agent.

Work only on the bounded assignment supplied by the parent.
Answer in the same language as the parent assignment unless it explicitly
requires another language.

You cannot see original images, screenshots or videos.
Use only explicit VISUAL_CONTEXT supplied by the parent.

Never invent visual observations.

If essential visual information is missing, return:
NEED_VISUAL_CLARIFICATION.

Do not spawn additional agents.

When an EVIDENCE PACKET is supplied, begin with that evidence. Expand reading
only when the packet is incomplete, conflicts with current files, or cannot
support the next decision. Do not rescan the repository by default.

Report tests and verification honestly; never claim a command or change was
executed when it was not.

If a BEGIN PARENT ASSIGNMENT / END PARENT ASSIGNMENT block exists,
treat it as the authoritative task contract. Dynamic Policy Execution
Contract, Model-specific Tuning (when present), and Stop Condition blocks
control how to execute that assignment without changing its scope or permissions.
"""

_AGENT_INSTRUCTIONS = {
    FLASH_ROLE: _FLASH_INSTRUCTIONS,
    PRO_ROLE: _PRO_INSTRUCTIONS,
}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_string_array(values: List[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _provider_auth_block() -> str:
    if platform_name() == "macos" and credential_backend() == "macos-keychain":
        helper = str(Path(__file__).resolve())
        python = sys.executable or "python3"
        return (
            "\n[model_providers.deepseek.auth]\n"
            f"command = {_toml_string(python)}\n"
            f"args = {_toml_string_array([helper, '_credential-get'])}\n"
            "timeout_ms = 5000\n"
            "refresh_interval_ms = 0\n"
        )
    return f'env_key = "{API_KEY_ENV}"\n'


def save_router_config(paths: Paths, config: RouterConfig) -> None:
    atomic_write(
        paths.settings,
        (json.dumps(settings_payload(config), ensure_ascii=False, indent=2) + "\n").encode(),
        mode=0o600,
    )


def _load_saved_router_config(paths: Paths) -> Optional[RouterConfig]:
    try:
        return load_router_config(paths.settings)
    except ProviderConfigError as exc:
        raise ManagerError("invalid_configuration", str(exc)) from exc


def _legacy_router_config(paths: Paths) -> Optional[RouterConfig]:
    if not paths.flash_agent.is_file() or not paths.pro_agent.is_file():
        return None
    try:
        flash = load_toml(paths.flash_agent.read_text(encoding="utf-8"))
        pro = load_toml(paths.pro_agent.read_text(encoding="utf-8"))
        flash_provider = flash["model_providers"][PROVIDER]
        pro_provider = pro["model_providers"][PROVIDER]
        flash_url = str(flash_provider["base_url"]).rstrip("/")
        pro_url = str(pro_provider["base_url"]).rstrip("/")
        if flash_url.endswith("/responses"):
            flash_url = flash_url[: -len("/responses")]
        if pro_url.endswith("/responses"):
            pro_url = pro_url[: -len("/responses")]
        if flash_url != pro_url:
            raise ManagerError(
                "configuration_conflict",
                "Legacy Flash and Pro Agent files use different base URLs.",
                {"flash_base_url": flash_url, "pro_base_url": pro_url},
            )
        return RouterConfig(
            base_url=flash_url,
            flash_model=str(flash["model"]),
            pro_model=str(pro["model"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManagerError(
            "configuration_conflict",
            "Legacy Agent files do not contain a compatible router configuration.",
        ) from exc


def resolve_router_config(
    paths: Paths,
    *,
    base_url: Optional[str] = None,
    flash_model: Optional[str] = None,
    pro_model: Optional[str] = None,
) -> RouterConfig:
    saved = _load_saved_router_config(paths)
    if saved is None:
        try:
            saved = _legacy_router_config(paths) or RouterConfig()
        except ManagerError as exc:
            # An explicit complete profile is the only safe way to replace
            # conflicting legacy files; partial overrides must not guess.
            if exc.code != "configuration_conflict" or not all(
                value is not None for value in (base_url, flash_model, pro_model)
            ):
                raise
            saved = RouterConfig()
    try:
        return RouterConfig(
            base_url=saved.base_url if base_url is None else base_url,
            flash_model=saved.flash_model if flash_model is None else flash_model,
            pro_model=saved.pro_model if pro_model is None else pro_model,
        )
    except ProviderConfigError as exc:
        raise ManagerError("invalid_configuration", str(exc)) from exc


def agent_toml_text(spec: AgentSpec, config: Optional[RouterConfig] = None) -> str:
    config = config or RouterConfig()
    model = config.flash_model if spec.role == FLASH_ROLE else config.pro_model
    instructions = _AGENT_INSTRUCTIONS[spec.role].strip()
    return (
        f'name = "{spec.role}"\n'
        f"description = {_toml_string(spec.description)}\n\n"
        f'model_provider = "{PROVIDER}"\n'
        f'model = {_toml_string(model)}\n'
        f"model_context_window = 1000000\n"
        f'sandbox_mode = "{spec.sandbox_mode}"\n\n'
        f"developer_instructions = {_toml_string(instructions)}\n\n"
        f"[model_providers.{PROVIDER}]\n"
        f'name = "DeepSeek"\n'
        f'base_url = {_toml_string(config.base_url)}\n'
        f'wire_api = "{WIRE_API}"\n'
        + _provider_auth_block()
    )


# ---------------------------------------------------------------------------
# Model catalog (dual registration, never one without the other)
# ---------------------------------------------------------------------------


def catalog_payload(config: Optional[RouterConfig] = None) -> Dict[str, Any]:
    config = config or RouterConfig()

    def entry(model: str, label: str, description: str) -> Dict[str, Any]:
        roles = []
        if model == config.flash_model:
            roles.append(FLASH_ROLE)
        if model == config.pro_model:
            roles.append(PRO_ROLE)
        return {
            "slug": model,
            "name": label,
            "description": description,
            "model_provider": PROVIDER,
            "base_url": config.base_url,
            "wire_api": WIRE_API,
            "context_window": 1000000,
            "router_roles": roles,
        }

    return {
        "models": [
            entry(
                config.flash_model,
                "DeepSeek V4 Flash",
                "Fast text-only DeepSeek model for bounded exploration, search, logs, extraction and pre-implementation analysis.",
            ),
            entry(
                config.pro_model,
                "DeepSeek V4 Pro",
                "Deep text-only DeepSeek model for root cause, architecture, concurrency, review and difficult implementation.",
            ),
        ]
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def read_manifest(paths: Paths) -> Dict[str, Any]:
    if not paths.manifest.is_file():
        return {}
    try:
        payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_manifest(paths: Paths, payload: Dict[str, Any]) -> None:
    atomic_write(paths.manifest, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())


def default_manifest(original_parent_model: str, original_parent_provider: Optional[str]) -> Dict[str, Any]:
    return {
        "schema_version": 2,
        "hash_version": HASH_VERSION_EXACT_BYTES,
        "project": PROJECT_NAME,
        "installed_at": datetime.now().isoformat(timespec="seconds"),
        "managed": {
            "flash_agent": True,
            "pro_agent": True,
            "provider": True,  # provider lives inside the managed agent TOMLs
            "catalog": True,
            "plugin": True,
        },
        "adopted_existing": {
            "provider": False,
            "catalog": False,
            "legacy_hook": False,
        },
        "original": {
            "parent_model": original_parent_model,
            "parent_provider": original_parent_provider,
        },
        "hashes": {},
        "preexisted": {"catalog": False},
        "transport_mode": TransportMode.PLAINTEXT_HOOK.value,
        "legacy_migration": None,
        "last_test": None,
    }


# ---------------------------------------------------------------------------
# Backup / transaction (ported from oil-oil)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagedAsset:
    path: Path
    verify_integrity: bool = True
    hook_runtime: bool = False
    shared: bool = False


def managed_assets(paths: Paths) -> Dict[str, ManagedAsset]:
    """Single registry for all managed paths and their lifecycle semantics."""
    return {
        "flash_agent": ManagedAsset(paths.flash_agent),
        "pro_agent": ManagedAsset(paths.pro_agent),
        "catalog": ManagedAsset(paths.catalog),
        # These are execution helpers for explicit Skill fallback. The Plugin
        # Hook itself always executes the Plugin-relative copies.
        "handoff_script_py": ManagedAsset(paths.hooks_install_dir / "plaintext_handoff.py"),
        "handoff_script_ps1": ManagedAsset(paths.hooks_install_dir / "plaintext-handoff.ps1"),
        "handoff_reasoning": ManagedAsset(
            paths.hooks_install_dir / "runtime" / "reasoning.py"
        ),
    }


def managed_asset_paths(paths: Paths) -> Dict[str, Path]:
    return {key: asset.path for key, asset in managed_assets(paths).items()}


def tracked_files(paths: Paths) -> List[Path]:
    """Every file the manager may write; all of them join the transaction snapshot."""
    return [paths.config, paths.manifest, paths.settings, *managed_asset_paths(paths).values()]


def make_backup(paths: Paths) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = paths.backups_dir / stamp
    backup.mkdir(parents=True, exist_ok=False)
    for source in tracked_files(paths):
        if source.is_file():
            shutil.copy2(source, backup / source.name)
    return backup


def restore_backup(paths: Paths, backup: Path) -> None:
    for target in tracked_files(paths):
        source = backup / target.name
        if source.is_file():
            mode = stat.S_IMODE(source.stat().st_mode)
            atomic_write(target, source.read_bytes(), mode=mode)
            shutil.copystat(source, target)
        elif target.is_file():
            target.unlink()


def try_acquire_file_lock(lock_file) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    if msvcrt is not None:
        lock_file.seek(0)
        try:
            first_byte = lock_file.read(1)
        except OSError:
            # Windows denies reads when another handle locks this byte.
            return False
        if first_byte == "":
            lock_file.seek(0)
            lock_file.write("\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    raise ManagerError("unsupported_platform", "No file locking implementation is available.")


def release_file_lock(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def operation_lock(paths: Paths, timeout_seconds: float = LOCK_WAIT_SECONDS):
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    with (paths.state_dir / "manager.lock").open("a+") as lock_file:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if try_acquire_file_lock(lock_file):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerError(
                    "operation_in_progress",
                    "Another DeepSeek router configuration operation is still running. Try again later.",
                )
            time.sleep(min(0.1, remaining))
        try:
            yield
        finally:
            release_file_lock(lock_file)


# ---------------------------------------------------------------------------
# Foreign config handling
# ---------------------------------------------------------------------------


def _file_is_ours(path: Path, manifest: Dict[str, Any], manifest_hash_key: str) -> bool:
    """True when the file is byte-identical to the version we installed."""
    expected = (manifest.get("hashes") or {}).get(manifest_hash_key)
    if not expected or not path.is_file():
        return False
    hash_version = manifest.get("hash_version")
    if hash_version == HASH_VERSION_EXACT_BYTES:
        return sha256_file(path) == expected
    # Manifests written before hash_version normalized line endings. Accept
    # that legacy ownership proof once so repair can migrate it safely.
    if hash_version is None:
        return sha256_text_file(path) == expected
    return False


def _assert_writable_target(
    path: Path,
    expected: bytes,
    manifest: Dict[str, Any],
    manifest_hash_key: str,
    restore_managed: bool = False,
) -> bool:
    """Returns True when the target may be written (missing, ours, or adoptable)."""
    if not path.is_file():
        return True
    if _file_is_ours(path, manifest, manifest_hash_key):
        return True
    if restore_managed and manifest_hash_key in (manifest.get("hashes") or {}):
        return True
    if sha256_file(path) == sha256_bytes(expected):
        return True  # identical content: adopt
    return False


# ---------------------------------------------------------------------------
# Installers
# ---------------------------------------------------------------------------


def install_agent(
    paths: Paths,
    spec: AgentSpec,
    manifest: Dict[str, Any],
    config: Optional[RouterConfig] = None,
    restore_managed: bool = False,
    allow_legacy_line_endings: bool = False,
) -> bool:
    """Write one agent TOML. Raises conflict on foreign content. Returns True when the file changed."""
    target = paths.agent_path(spec.role)
    text = agent_toml_text(spec, config)
    data = text.encode()
    hash_key = "flash_agent" if spec.role == FLASH_ROLE else "pro_agent"
    writable = not target.is_file() or _assert_writable_target(
        target, data, manifest, hash_key, restore_managed
    )
    tracked_hash = (manifest.get("hashes") or {}).get(hash_key)
    if not writable and allow_legacy_line_endings and not tracked_hash:
        try:
            writable = target.read_text(encoding="utf-8") == text
        except (OSError, UnicodeError):
            writable = False
    if not writable:
        raise ManagerError(
            "conflict",
            f"Existing agent file differs from the router-managed target: {target}",
            {"path": str(target)},
        )
    if target.is_file() and target.read_text(encoding="utf-8") == text:
        return False
    atomic_write(target, data, mode=0o644)
    return True


def install_catalog(
    paths: Paths,
    manifest: Dict[str, Any],
    config: Optional[RouterConfig] = None,
    restore_managed: bool = False,
) -> bool:
    """Write the dual-model catalog. Foreign existing content is a conflict, never overwritten."""
    data = (json.dumps(catalog_payload(config), ensure_ascii=False, indent=2) + "\n").encode()
    if paths.catalog.is_file() and not _assert_writable_target(
        paths.catalog, data, manifest, "catalog", restore_managed
    ):
        raise ManagerError(
            "conflict",
            f"Existing model catalog differs from the router-managed target: {paths.catalog}",
            {"path": str(paths.catalog)},
        )
    if paths.catalog.is_file() and paths.catalog.read_text(encoding="utf-8") == data.decode("utf-8"):
        return False
    atomic_write(paths.catalog, data)
    return True


def _legacy_catalog_is_router_owned(path: Path, config: RouterConfig) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload in (catalog_payload(), catalog_payload(config))


def hook_entry_json(command: str, status_message: str) -> Dict[str, Any]:
    return {
        "type": "command",
        "command": command,
        "timeout": 10,
        "statusMessage": status_message,
        "additionalContextLimit": 0,
    }


def our_hook_config(paths: Paths) -> Dict[str, Any]:
    """Legacy global Hook shape used only for detection and precise migration."""
    python = sys.executable or "python3"
    handoff_script = paths.hooks_install_dir / "plaintext_handoff.py"
    command = (
        f'{python} "{handoff_script}" --mode hook --state-directory "{paths.handoff_dir}"'
    )
    entry = hook_entry_json(command, "正在传递 DeepSeek 子 Agent 任务 / Delivering assignment")
    if platform_name() == "windows":
        ps1_script = paths.hooks_install_dir / "plaintext-handoff.ps1"
        windows_command = (
            'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-File "{ps1_script}" -Mode hook -StateDirectory "{paths.handoff_dir}"'
        )
        entry["commandWindows"] = windows_command
    return {
        "description": "Plaintext task handoff for DeepSeek child agents.",
        "hooks": {
            "SubagentStart": [
                {
                    "matcher": "^(deepseek_flash|deepseek_pro)$",
                    "hooks": [entry],
                }
            ]
        },
    }


def plugin_hook_config(paths: Paths) -> Dict[str, Any]:
    """Load the Plugin-owned Hook without mutating Codex configuration."""
    try:
        value = json.loads(paths.plugin_hooks_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def plugin_hook_available(paths: Paths) -> bool:
    definition = plugin_hook_definition(paths)
    if definition is None:
        return False
    entry, handler = definition
    command = handler.get("command")
    return (
        handler.get("type") == "command"
        and isinstance(command, str)
        and "PLUGIN_ROOT" in command
        and handler.get("timeout") == 10
    )


def plugin_hook_definition(paths: Paths) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Return the single Plugin Hook definition when its JSON shape is valid."""
    config = plugin_hook_config(paths)
    hooks = config.get("hooks") if isinstance(config, dict) else None
    if not isinstance(hooks, dict):
        return None
    entries = hooks.get("SubagentStart")
    if not isinstance(entries, list) or len(entries) != 1:
        return None
    entry = entries[0]
    if not isinstance(entry, dict) or entry.get("matcher") != "^(deepseek_flash|deepseek_pro)$":
        return None
    handlers = entry.get("hooks")
    if not isinstance(handlers, list) or len(handlers) != 1 or not isinstance(handlers[0], dict):
        return None
    return entry, handlers[0]


def _matcher_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    def normalize(entry: Dict[str, Any]) -> Dict[str, Any]:
        copied = dict(entry)
        copied.pop("statusMessage", None)
        copied.pop("description", None)
        return copied

    return normalize(a) == normalize(b)


def _entry_is_ours(entry: Dict[str, Any], paths: Paths) -> bool:
    """Recognize a previously installed router entry even when the command
    path changed (interpreter upgrade, moved skill directory)."""
    inner = (entry.get("hooks") or [{}])[0] if isinstance(entry, dict) else {}
    command = inner.get("command", "") if isinstance(inner, dict) else ""
    command_windows = inner.get("commandWindows", "") if isinstance(inner, dict) else ""

    def basename(value: str) -> str:
        return re.split(r"[\\/]", value.strip('"'))[-1].lower()

    recognized = False
    def owned_script(path_value: str, expected_name: str) -> bool:
        candidate = Path(path_value).expanduser()
        if not candidate.is_file() or candidate.name.lower() != expected_name.lower():
            return False
        source = package_root() / "hooks" / expected_name
        try:
            return source.is_file() and sha256_file(candidate) == sha256_file(source)
        except OSError:
            return False

    if command:
        if re.search(r"[;&|`$><\r\n]", command):
            return False
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
        if not (
            len(tokens) == 6
            and basename(tokens[1]) == "plaintext_handoff.py"
            and tokens[2:5] == ["--mode", "hook", "--state-directory"]
            and tokens[0]
            and tokens[5]
            and owned_script(tokens[1], "plaintext_handoff.py")
        ):
            return False
        recognized = True
    if command_windows:
        if re.search(r"[;&|`$><\r\n]", command_windows):
            return False
        try:
            tokens = [token.strip('"') for token in shlex.split(command_windows, posix=False)]
        except ValueError:
            return False
        lowered = [token.lower() for token in tokens]
        if not (
            len(tokens) == 11
            and basename(tokens[0]) == "powershell.exe"
            and lowered[1:6] == [
                "-noprofile",
                "-noninteractive",
                "-executionpolicy",
                "bypass",
                "-file",
            ]
            and basename(tokens[6]) == "plaintext-handoff.ps1"
            and lowered[7:10] == ["-mode", "hook", "-statedirectory"]
            and tokens[10]
            and owned_script(tokens[6], "plaintext-handoff.ps1")
        ):
            return False
        recognized = True
    return recognized


def merge_hook_config(existing: Dict[str, Any], ours: Dict[str, Any], paths: Paths) -> Tuple[Dict[str, Any], bool]:
    """Merge our SubagentStart entry into an existing hooks.json, preserving unrelated hooks.

    Returns (merged, adopted) where adopted is True when an equivalent entry already existed.
    """
    merged = json.loads(json.dumps(existing))
    existing_hooks = merged.get("hooks", {}) if isinstance(merged, dict) else {}
    if not isinstance(existing_hooks, dict):
        raise ManagerError("conflict", "Existing hooks.json has an invalid 'hooks' section.", {})
    subagent_entries = existing_hooks.get("SubagentStart", [])
    if not isinstance(subagent_entries, list):
        raise ManagerError("conflict", "Existing hooks.json 'SubagentStart' section is not a list.", {})
    our_matcher = ours["hooks"]["SubagentStart"][0]
    for index, existing_entry in enumerate(subagent_entries):
        if not isinstance(existing_entry, dict):
            continue
        if existing_entry.get("matcher") == our_matcher["matcher"]:
            if _matcher_equal(existing_entry, our_matcher):
                return existing, True  # adopt: equivalent entry already installed
            if _entry_is_ours(existing_entry, paths):
                subagent_entries[index] = our_matcher  # refresh our own entry in place
                return merged, True
            raise ManagerError(
                "conflict",
                "An existing SubagentStart hook uses the deepseek_flash/deepseek_pro matcher "
                "but differs from the router-managed entry. It will not be overwritten.",
                {"matcher": our_matcher["matcher"]},
            )
    merged.setdefault("hooks", {})
    merged["hooks"].setdefault("SubagentStart", [])
    merged["hooks"]["SubagentStart"].append(our_matcher)
    return merged, False


def install_hook_files(
    paths: Paths,
    manifest: Dict[str, Any],
    restore_managed: bool = False,
) -> None:
    """Copy the handoff helpers into the Codex home. Foreign files are conflicts."""
    source_dir = package_root() / "hooks"
    paths.hooks_install_dir.mkdir(parents=True, exist_ok=True)
    install_specs = (
        (
            source_dir / "plaintext_handoff.py",
            paths.hooks_install_dir / "plaintext_handoff.py",
            "handoff_script_py",
        ),
        (
            source_dir / "plaintext-handoff.ps1",
            paths.hooks_install_dir / "plaintext-handoff.ps1",
            "handoff_script_ps1",
        ),
        (
            package_root() / "runtime" / "reasoning.py",
            paths.hooks_install_dir / "runtime" / "reasoning.py",
            "handoff_reasoning",
        ),
    )
    for source, target, hash_key in install_specs:
        if not source.is_file():
            continue
        data = source.read_bytes()
        if target.is_file() and not _assert_writable_target(
            target, data, manifest, hash_key, restore_managed
        ):
            raise ManagerError(
                "conflict",
                f"Existing hook script differs from the router-managed target: {target}",
                {"path": str(target)},
            )
        atomic_write(target, data, mode=0o644)


def install_hook_config(paths: Paths, manifest: Dict[str, Any]) -> Tuple[bool, bool]:
    """Install the handoff scripts and merge the hook entry into hooks.json.

    Returns (changed, adopted_existing).
    """
    if paths.config.is_file():
        if toml_has_table(paths.config.read_text(encoding="utf-8"), "hooks"):
            raise ManagerError(
                "inline_hook_config_unsupported",
                "config.toml already contains inline hook configuration. This router does not "
                "merge into inline hook tables; migrate them to hooks.json first or review /hooks.",
            )
    install_hook_files(paths, manifest)
    ours = our_hook_config(paths)
    existing: Dict[str, Any] = {}
    if paths.hooks_config.is_file():
        try:
            existing = json.loads(paths.hooks_config.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManagerError("conflict", f"Existing hooks.json is not valid JSON: {exc}") from exc
        if not isinstance(existing, dict):
            raise ManagerError("conflict", "Existing hooks.json is not a JSON object.", {})
        # hooks.json is always shared, even when its last observed bytes match
        # our manifest. Preserve unrelated entries on every setup/repair.
        merged, adopted = merge_hook_config(existing, ours, paths)
        data = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode()
        changed = sha256_file(paths.hooks_config) != sha256_bytes(data)
        if changed:
            atomic_write(paths.hooks_config, data)
        return changed, adopted
    data = (json.dumps(ours, ensure_ascii=False, indent=2) + "\n").encode()
    changed = (
        not paths.hooks_config.is_file()
        or paths.hooks_config.read_text(encoding="utf-8") != data.decode("utf-8")
    )
    if changed:
        atomic_write(paths.hooks_config, data)
    return changed, False


def _wait_for_app_server_response(
    messages: "queue.Queue[Optional[Dict[str, Any]]]",
    request_id: int,
    deadline: float,
) -> Optional[Dict[str, Any]]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            message = messages.get(timeout=remaining)
        except queue.Empty:
            return None
        if message is None:
            return None
        if message.get("id") != request_id:
            continue
        if message.get("error") is not None:
            return None
        result_value = message.get("result")
        return result_value if isinstance(result_value, dict) else None


def _query_codex_hooks(paths: Paths, codex_bin: str) -> Optional[Dict[str, Any]]:
    """Ask Codex for its canonical discovered-hook and trust metadata.

    Hook trust is keyed by source identity plus Codex's current normalized hash.
    Reading config.toml cannot safely reconstruct whether that hash still matches,
    so status checks use the same app-server hooks/list API as the /hooks TUI.
    """
    workspace = Path.cwd().resolve()
    env = dict(os.environ)
    env["CODEX_HOME"] = str(paths.codex_home)
    try:
        process = subprocess.Popen(
            [codex_bin, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            bufsize=1,
        )
    except (OSError, ValueError):
        return None

    messages: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()

    def read_messages() -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    messages.put(value)
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_messages, daemon=True)
    reader.start()

    def send(message: Dict[str, Any]) -> bool:
        if process.stdin is None:
            return False
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            return False
        return True

    deadline = time.monotonic() + APP_SERVER_TIMEOUT_SECONDS
    try:
        initialized = send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": PROJECT_NAME.replace("-", "_"),
                        "title": "Codex DeepSeek Router",
                        "version": "1",
                    }
                },
            }
        ) and _wait_for_app_server_response(messages, 0, deadline)
        if not initialized:
            return None
        if not send({"method": "initialized", "params": {}}):
            return None
        if not send(
            {
                "method": "hooks/list",
                "id": 1,
                "params": {"cwds": [str(workspace)]},
            }
        ):
            return None
        response = _wait_for_app_server_response(messages, 1, deadline)
        if response is None:
            return None
        for entry in response.get("data") or []:
            if not isinstance(entry, dict):
                continue
            try:
                same_cwd = Path(entry.get("cwd", "")).resolve() == workspace
            except (OSError, RuntimeError, TypeError):
                same_cwd = False
            if same_cwd:
                return entry
        return None
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def hook_trusted(paths: Paths, codex_bin: Optional[str] = None) -> bool:
    """True only when Codex reports the Plugin Hook source as trusted.

    Codex may discover a Plugin from its managed cache rather than from the
    checkout containing this manager script.  In that case ``hooks/list``
    reports the cached source path and the Plugin ID, while the hook contract
    (event, matcher and command) remains the same.  Trust is therefore keyed
    by Codex's canonical Plugin identity plus the exact managed hook shape,
    not by the manager checkout path alone.
    """
    if not codex_bin:
        return False
    runtime_entry = _query_codex_hooks(paths, codex_bin)
    if not runtime_entry or runtime_entry.get("errors"):
        return False

    definition = plugin_hook_definition(paths)
    if definition is None:
        return False
    group, handler = definition
    expected_command = handler.get("commandWindows") if platform_name() == "windows" else handler.get("command")
    expanded_command = str(expected_command or "").replace("$PLUGIN_ROOT", str(paths.plugin_root))
    try:
        expected_source = paths.plugin_hooks_config.resolve()
    except (OSError, RuntimeError):
        expected_source = paths.plugin_hooks_config.absolute()

    expected_plugin_name = None
    try:
        manifest = json.loads(paths.plugin_manifest.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
            expected_plugin_name = manifest["name"]
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass

    matches: List[Dict[str, Any]] = []
    for hook in runtime_entry.get("hooks") or []:
        if not isinstance(hook, dict):
            continue
        try:
            source_matches = Path(hook.get("sourcePath", "")).resolve() == expected_source
        except (OSError, RuntimeError, TypeError):
            source_matches = False
        plugin_id = hook.get("pluginId")
        plugin_name = plugin_id.split("@", 1)[0] if isinstance(plugin_id, str) else None
        plugin_identity_matches = (
            hook.get("source") == "plugin"
            and expected_plugin_name is not None
            and plugin_name == expected_plugin_name
        )
        if (
            (source_matches or plugin_identity_matches)
            and hook.get("eventName") == "subagentStart"
            and hook.get("handlerType") == "command"
            and hook.get("matcher") == group["matcher"]
            and hook.get("command") in {expected_command, expanded_command}
        ):
            matches.append(hook)

    return bool(
        len(matches) == 1
        and matches[0].get("enabled") is True
        and matches[0].get("trustStatus") == "trusted"
    )


def hook_entry_present(paths: Paths) -> bool:
    """True when the legacy global hooks.json contains our router entry."""
    if not paths.hooks_config.is_file():
        return False
    try:
        config = json.loads(paths.hooks_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    entries = (config.get("hooks") or {}).get("SubagentStart") if isinstance(config, dict) else None
    if not isinstance(entries, list):
        return False
    ours = our_hook_config(paths)["hooks"]["SubagentStart"][0]
    router_entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("matcher") == ours["matcher"]
    ]
    return len(router_entries) == 1 and _matcher_equal(router_entries[0], ours)


def legacy_hook_status(paths: Paths) -> Dict[str, Any]:
    """Describe only the old global entry this project can identify exactly."""
    if not paths.hooks_config.is_file():
        return {"present": False, "recognized": False, "conflict": False}
    try:
        config = json.loads(paths.hooks_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": True, "recognized": False, "conflict": True}
    entries = (config.get("hooks") or {}).get("SubagentStart") if isinstance(config, dict) else None
    matching = [entry for entry in entries or [] if isinstance(entry, dict) and entry.get("matcher") == "^(deepseek_flash|deepseek_pro)$"]
    recognized = [entry for entry in matching if _entry_is_ours(entry, paths)]
    return {
        "present": bool(matching),
        "recognized": len(recognized) == len(matching) and bool(recognized),
        "conflict": bool(matching) and len(recognized) != len(matching),
        "count": len(matching),
    }


def plugin_status(paths: Paths) -> Dict[str, Any]:
    manifest_valid = False
    if paths.plugin_manifest.is_file():
        try:
            payload = json.loads(paths.plugin_manifest.read_text(encoding="utf-8"))
            manifest_valid = (
                isinstance(payload, dict)
                and payload.get("name") == PROJECT_NAME
                and isinstance(payload.get("version"), str)
            )
        except (OSError, json.JSONDecodeError):
            manifest_valid = False
    return {
        "root": str(paths.plugin_root),
        "manifest": str(paths.plugin_manifest),
        "manifest_valid": manifest_valid,
        "hook_config": str(paths.plugin_hooks_config),
        "hook_available": plugin_hook_available(paths),
    }


def runtime_assets_valid(paths: Paths, manifest: Dict[str, Any]) -> bool:
    """All executable/runtime assets must match the installed manifest exactly."""
    return all(
        _file_is_ours(asset.path, manifest, key)
        for key, asset in managed_assets(paths).items()
        if asset.verify_integrity
    )


def hook_files_installed(paths: Paths, manifest: Dict[str, Any]) -> bool:
    helper_keys = ("handoff_script_py", "handoff_script_ps1", "handoff_reasoning")
    assets = managed_assets(paths)
    return plugin_hook_available(paths) and all(
        assets[key].path.is_file() for key in helper_keys
    )


def apply_managed_assets(
    paths: Paths,
    manifest: Dict[str, Any],
    config: Optional[RouterConfig] = None,
    restore_managed: bool = False,
    allow_legacy_agent_line_endings: bool = False,
) -> Tuple[Dict[str, bool], bool]:
    """Install/refresh non-Plugin assets. Plugin Skills and Hooks stay in-place."""
    changed: Dict[str, bool] = {}
    changed["catalog"] = install_catalog(paths, manifest, config, restore_managed)
    changed["flash_agent"] = install_agent(
        paths,
        AGENT_SPECS[FLASH_ROLE],
        manifest,
        config,
        restore_managed,
        allow_legacy_agent_line_endings,
    )
    changed["pro_agent"] = install_agent(
        paths,
        AGENT_SPECS[PRO_ROLE],
        manifest,
        config,
        restore_managed,
        allow_legacy_agent_line_endings,
    )
    handoff_paths = {
        key: path
        for key, path in managed_asset_paths(paths).items()
        if key.startswith("handoff_")
    }
    before = {
        key: sha256_file(path) if path.is_file() else None
        for key, path in handoff_paths.items()
    }
    install_hook_files(paths, manifest, restore_managed)
    changed["handoff_runtime"] = any(
        before[key] != sha256_file(path) for key, path in handoff_paths.items()
    )
    return changed, False


def compute_asset_hashes(paths: Paths) -> Dict[str, str]:
    return {
        key: sha256_file(path)
        for key, path in managed_asset_paths(paths).items()
    }


# ---------------------------------------------------------------------------
# Static status
# ---------------------------------------------------------------------------


def parent_config_snapshot(paths: Paths) -> Dict[str, Optional[str]]:
    if not paths.config.is_file():
        return {"parent_model": None, "parent_provider": None}
    text = paths.config.read_text(encoding="utf-8")
    return {
        "parent_model": toml_get_top_level_string(text, "model"),
        "parent_provider": toml_get_top_level_string(text, "model_provider"),
    }


def agent_status(
    paths: Paths,
    manifest: Dict[str, Any],
    spec: AgentSpec,
    config: Optional[RouterConfig] = None,
) -> Dict[str, Any]:
    target = paths.agent_path(spec.role)
    hash_key = "flash_agent" if spec.role == FLASH_ROLE else "pro_agent"
    valid = target.is_file() and target.read_text(encoding="utf-8") == agent_toml_text(spec, config)
    config = config or RouterConfig()
    model = config.flash_model if spec.role == FLASH_ROLE else config.pro_model
    return {
        "installed": target.is_file(),
        "valid": valid,
        "managed": _file_is_ours(target, manifest, hash_key),
        "model": model,
    }


def catalog_status(paths: Paths, config: Optional[RouterConfig] = None) -> Dict[str, Any]:
    config = config or RouterConfig()
    registered = False
    if paths.catalog.is_file():
        try:
            data = json.loads(paths.catalog.read_text(encoding="utf-8"))
            entries = {
                item.get("slug"): item
                for item in data.get("models", [])
                if isinstance(item, dict)
            }
            registered = all(
                entries.get(model, {}).get("base_url") == config.base_url
                and entries.get(model, {}).get("wire_api") == config.wire_api
                and role in entries.get(model, {}).get("router_roles", [])
                for model, role in (
                    (config.flash_model, FLASH_ROLE),
                    (config.pro_model, PRO_ROLE),
                )
            )
        except (OSError, json.JSONDecodeError):
            registered = False
    return {"path": str(paths.catalog), "registered": registered}


def static_status(paths: Paths, codex_bin: Optional[str] = None) -> Dict[str, Any]:
    manifest = read_manifest(paths)
    installed = bool(manifest)
    config_error: Optional[str] = None
    try:
        config = resolve_router_config(paths)
    except ManagerError as exc:
        config = RouterConfig()
        config_error = str(exc)
    snapshot = parent_config_snapshot(paths)
    original = manifest.get("original") or {}
    parent_unchanged = (
        installed
        and original.get("parent_model") == snapshot["parent_model"]
        and original.get("parent_provider", None) == snapshot.get("parent_provider", None)
    )

    checks: Dict[str, Any] = {
        "flash_agent": agent_status(paths, manifest, AGENT_SPECS[FLASH_ROLE], config),
        "pro_agent": agent_status(paths, manifest, AGENT_SPECS[PRO_ROLE], config),
        "catalog": catalog_status(paths, config),
    }
    legacy = legacy_hook_status(paths)
    plugin = plugin_status(paths)
    hooks_installed = hook_files_installed(paths, manifest)
    assets_valid = runtime_assets_valid(paths, manifest)
    trusted = hook_trusted(paths, codex_bin)
    errors: List[str] = []
    if config_error:
        errors.append(config_error)

    codex_path: Optional[str] = None
    codex_version: Optional[str] = None
    if codex_bin:
        try:
            codex_version = codex_version_text(codex_bin)
            codex_path = codex_bin
        except ManagerError as exc:
            errors.append(str(exc))

    backup_count = 0
    if paths.backups_dir.is_dir():
        backup_count = sum(1 for entry in paths.backups_dir.iterdir() if entry.is_dir())

    last_test = manifest.get("last_test")
    test_evidence = bool(
        last_test and last_test.get("flash") and last_test.get("pro")
    )
    # Live-test evidence stays valid only while the managed files it tested
    # are still byte-identical to the installed versions.
    evidence_fresh = test_evidence and assets_valid
    credential_is_present = credential_present()

    if installed:
        static_ok = (
            checks["flash_agent"]["valid"]
            and checks["pro_agent"]["valid"]
            and checks["catalog"]["registered"]
            and assets_valid
            and credential_is_present
            and parent_unchanged
            and not config_error
        )
        if manifest.get("disabled") or manifest.get("automatic_disabled"):
            status = "disabled"
        elif static_ok and evidence_fresh:
            status = "ready"
        elif static_ok:
            status = "configured"
        else:
            status = "partial"
    else:
        status = "not_installed"

    return result(
        status,
        installed=installed,
        runtime={
            "platform": platform_name(),
            "python": sys.version.split()[0],
            "codex_path": codex_path,
            "codex_version": codex_version,
            "codex_detected": codex_path is not None,
        },
        parent={
            "model": snapshot["parent_model"],
            "provider": snapshot["parent_provider"],
            "unchanged": parent_unchanged,
        },
        agents={
            FLASH_ROLE: checks["flash_agent"],
            PRO_ROLE: checks["pro_agent"],
        },
        provider={
            "registered": checks["flash_agent"]["valid"] and checks["pro_agent"]["valid"],
            "top_level_untouched": parent_unchanged,
            "base_url": config.base_url,
            "wire_api": config.wire_api,
            "flash_model": config.flash_model,
            "pro_model": config.pro_model,
            "settings": str(paths.settings),
        },
        credential={"backend": credential_backend(), "present": credential_is_present},
        hook={
            "installed": hooks_installed,
            "entry_present": hooks_installed,
            "files_installed": hooks_installed,
            "trusted": trusted,
            "review_required": installed and hooks_installed and not trusted,
            "automatic": hooks_installed and trusted,
            "legacy": legacy,
        },
        plugin=plugin,
        hook_trusted=trusted,
        transport_mode=choose_transport(False, hooks_installed, codex_version).value,
        catalog=checks["catalog"],
        backup={"count": backup_count},
        last_test=last_test,
        errors=errors,
    )


def validate_static_configuration(paths: Paths) -> None:
    status = static_status(paths)
    if status["status"] not in {"configured", "disabled", "ready"}:
        problems = [
            name for name, value in status["agents"].items() if not value["valid"]
        ]
        if not status["catalog"]["registered"]:
            problems.append("catalog")
        if not status["hook"]["installed"]:
            problems.append("hook")
        raise ManagerError(
            "static_validation_failed",
            f"Static validation failed: {', '.join(problems)}",
            status,
        )


# ---------------------------------------------------------------------------
# Lifecycle commands
# ---------------------------------------------------------------------------


def setup(
    paths: Paths,
    codex_bin: str,
    api_key_stdin: bool,
    skip_live_test: bool,
    base_url: Optional[str] = None,
    flash_model: Optional[str] = None,
    pro_model: Optional[str] = None,
) -> Dict[str, Any]:
    if not credential_present():
        if not api_key_stdin:
            return result(
                "credential_missing",
                message="No DeepSeek API key is available. Pipe it through --api-key-stdin.",
                credential="deepseek_api_key",
            )
        secret = sys.stdin.readline().strip()
        if not secret:
            raise ManagerError("credential_missing", "Standard input contained no API key.")
        store_api_key(secret)
        secret = ""

    config = resolve_router_config(
        paths,
        base_url=base_url,
        flash_model=flash_model,
        pro_model=pro_model,
    )
    manifest = read_manifest(paths)
    previous = manifest or {}
    allow_legacy_agent_line_endings = (
        not manifest
        and not paths.settings.is_file()
        and paths.flash_agent.is_file()
        and paths.pro_agent.is_file()
    )
    if (
        not manifest
        and not paths.settings.is_file()
        and _legacy_catalog_is_router_owned(paths.catalog, config)
    ):
        previous = {
            "hash_version": HASH_VERSION_EXACT_BYTES,
            "hashes": {"catalog": sha256_file(paths.catalog)},
        }
    adopted: Dict[str, bool] = {
        "catalog": bool(paths.catalog.is_file()),
        "legacy_hook": legacy_hook_status(paths).get("recognized", False),
    }
    backup = make_backup(paths)
    changed: Dict[str, bool] = {}
    try:
        # 1. Codex environment
        detect_version = codex_version_text(codex_bin)
        # 2/3. Parent isolation snapshot
        snapshot = parent_config_snapshot(paths)
        if not snapshot["parent_model"]:
            raise ManagerError(
                "parent_model_unconfigured",
                "config.toml has no explicit top-level non-DeepSeek parent model.",
            )
        # 4-6. Managed assets: catalog and both agents. Plugin Skills/Hooks are
        # discovered from the installed Plugin and are never copied globally.
        changed, _ = apply_managed_assets(
            paths,
            previous,
            config,
            allow_legacy_agent_line_endings=allow_legacy_agent_line_endings,
        )
        previous_settings = paths.settings.read_bytes() if paths.settings.is_file() else None
        save_router_config(paths, config)
        changed["settings"] = previous_settings != paths.settings.read_bytes()

        new_manifest = default_manifest(snapshot["parent_model"], snapshot["parent_provider"])
        new_manifest["adopted_existing"].update(
            {
                "provider": False,
                "catalog": adopted["catalog"],
            "legacy_hook": adopted["legacy_hook"],
            }
        )
        new_manifest["preexisted"] = {
            "catalog": adopted["catalog"],
        }
        new_manifest["hashes"] = compute_asset_hashes(paths)
        write_manifest(paths, new_manifest)
        validate_static_configuration(paths)
    except Exception:
        restore_backup(paths, backup)
        raise

    payload = result(
        "configured",
        changed=changed,
        adopted_existing=adopted,
        codex_version=detect_version,
        backup=str(backup),
        restart_required=True,
        hook_review_required=plugin_hook_available(paths) and not hook_trusted(paths, codex_bin),
        new_task_required=True,
        data_boundary=(
            "Task text, related code context and tool results sent to the DeepSeek child "
            "agents are transmitted to the DeepSeek provider. Environment files, tokens, "
            "passwords and private keys are never placed in the handoff by this tool."
        ),
    )
    if platform_name() == "windows":
        payload["desktop_env_required"] = API_KEY_ENV
        payload["message"] = (
            "The Windows agent templates authenticate via the DEEPSEEK_API_KEY environment "
            "variable. Set it as a user environment variable and fully restart the Codex "
            "desktop app so the runtime inherits it."
        )
    elif platform_name() == "linux":
        payload["desktop_env_required"] = API_KEY_ENV
    if not skip_live_test:
        payload["message"] = (
            payload.get("message", "")
            + " Live smoke tests are not run by setup; run `test` afterwards."
        ).strip()
    return payload


def repair(paths: Paths, codex_bin: str) -> Dict[str, Any]:
    manifest = read_manifest(paths)
    if not manifest:
        raise ManagerError("not_managed", "No router manifest found. Run setup first.")
    config = resolve_router_config(paths)
    manifest["disabled"] = False
    manifest["automatic_disabled"] = False
    backup = make_backup(paths)
    try:
        changed, _ = apply_managed_assets(paths, manifest, config, restore_managed=True)
        previous_settings = paths.settings.read_bytes() if paths.settings.is_file() else None
        save_router_config(paths, config)
        changed["settings"] = previous_settings != paths.settings.read_bytes()
        manifest["adopted_existing"] = manifest.get("adopted_existing", {})
        manifest["hashes"] = compute_asset_hashes(paths)
        manifest["hash_version"] = HASH_VERSION_EXACT_BYTES
        # Repair may refresh executable assets or routing configuration. Prior
        # live-smoke evidence cannot certify the repaired installation.
        manifest["last_test"] = None
        # Parent model may have changed (user upgraded); refresh the recorded snapshot.
        snapshot = parent_config_snapshot(paths)
        manifest["original"] = {
            "parent_model": snapshot["parent_model"],
            "parent_provider": snapshot["parent_provider"],
        }
        write_manifest(paths, manifest)
        validate_static_configuration(paths)
    except Exception:
        restore_backup(paths, backup)
        raise
    return result(
        "configured",
        changed=changed,
        backup=str(backup),
        restart_required=True,
        hook_review_required=plugin_hook_available(paths) and not hook_trusted(paths, codex_bin),
        new_task_required=True,
    )


def remove_our_hook_entry(paths: Paths, manifest: Dict[str, Any]) -> bool:
    """Remove only the router's SubagentStart entry from hooks.json. Returns True when changed."""
    if not paths.hooks_config.is_file():
        return False
    try:
        config = json.loads(paths.hooks_config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManagerError("conflict", f"hooks.json is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ManagerError("conflict", "hooks.json is not a JSON object.", {})
    entries = (config.get("hooks") or {}).get("SubagentStart")
    if not isinstance(entries, list):
        return False
    matcher = "^(deepseek_flash|deepseek_pro)$"
    ours = our_hook_config(paths)["hooks"]["SubagentStart"][0]
    kept: List[Dict[str, Any]] = []
    changed = False
    for entry in entries:
        if isinstance(entry, dict) and entry.get("matcher") == matcher:
            if not (_matcher_equal(entry, ours) or _entry_is_ours(entry, paths)):
                raise ManagerError(
                    "conflict",
                    "The router hook entry in hooks.json was modified by the user; refusing to touch it.",
                    {"matcher": matcher},
                )
            changed = True
            continue
        kept.append(entry)
    if not changed:
        return False
    config["hooks"]["SubagentStart"] = kept
    if not kept:
        config["hooks"].pop("SubagentStart", None)
    if not config["hooks"]:
        config.pop("hooks", None)
    atomic_write(paths.hooks_config, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())
    return True


def disable(paths: Paths) -> Dict[str, Any]:
    manifest = read_manifest(paths)
    if not manifest:
        raise ManagerError("not_managed", "No router manifest found. Run setup first.")
    backup = make_backup(paths)
    manifest["automatic_disabled"] = True
    write_manifest(paths, manifest)
    return result(
        "disabled",
        changed=False,
        plugin_managed=True,
        message="Automatic routing remains owned by the Plugin. Disable or remove the Plugin in Codex to stop its Hook.",
        credential_preserved=credential_present(),
        catalog_preserved=paths.catalog.is_file(),
        backup=str(backup),
    )


def migrate_legacy(paths: Paths) -> Dict[str, Any]:
    """Remove only a recognized Skill-first global Hook and its owned scripts."""
    legacy = legacy_hook_status(paths)
    if legacy.get("conflict"):
        raise ManagerError(
            "conflict",
            "A DeepSeek matcher exists in hooks.json but is not an exact router-managed entry; refusing to remove it.",
            legacy,
        )
    backup = make_backup(paths)
    legacy_config_bytes = paths.hooks_config.read_bytes() if paths.hooks_config.is_file() else None
    legacy_config_mode = stat.S_IMODE(paths.hooks_config.stat().st_mode) if paths.hooks_config.is_file() else 0o600
    changed = False
    try:
        if legacy.get("recognized"):
            changed = remove_our_hook_entry(paths, read_manifest(paths))
            for target, source_name in (
                (paths.hooks_install_dir / "plaintext_handoff.py", "plaintext_handoff.py"),
                (paths.hooks_install_dir / "plaintext-handoff.ps1", "plaintext-handoff.ps1"),
            ):
                source = package_root() / "hooks" / source_name
                if target.is_file() and source.is_file() and sha256_file(target) == sha256_file(source):
                    target.unlink()
            if paths.hooks_install_dir.is_dir() and not any(paths.hooks_install_dir.iterdir()):
                paths.hooks_install_dir.rmdir()
        manifest = read_manifest(paths)
        if manifest:
            manifest["legacy_migration"] = {"recognized": bool(legacy.get("recognized")), "changed": changed}
            write_manifest(paths, manifest)
    except Exception:
        restore_backup(paths, backup)
        if legacy_config_bytes is not None:
            atomic_write(paths.hooks_config, legacy_config_bytes, mode=legacy_config_mode)
        raise
    return result(
        "migrated",
        changed=changed,
        legacy=legacy,
        backup=str(backup),
    )


def uninstall(paths: Paths, remove_credential: bool) -> Dict[str, Any]:
    manifest = read_manifest(paths)
    if not manifest:
        raise ManagerError("not_managed", "No router manifest found. Refusing to modify configuration.")
    asset_specs = managed_assets(paths)
    assets = {key: asset.path for key, asset in asset_specs.items()}
    for key, asset in asset_specs.items():
        if asset.shared:
            continue  # shared file: remove only our exact entry below
        target = asset.path
        if target.is_file() and not _file_is_ours(target, manifest, key):
            raise ManagerError(
                "conflict",
                f"Managed file was modified since installation; refusing to remove it: {target}",
                {"path": str(target)},
            )
    backup = make_backup(paths)
    try:
        # Plugin-owned Skills and Hooks are removed by Codex Plugin uninstall.
        # The manager only removes its global Agents/catalog and state.
        for target in (assets["flash_agent"], assets["pro_agent"]):
            target.unlink(missing_ok=True)
        for key in ("handoff_script_py", "handoff_script_ps1", "handoff_reasoning"):
            assets[key].unlink(missing_ok=True)
        for directory in (paths.hooks_install_dir / "runtime", paths.hooks_install_dir):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        catalog_removed = False
        catalog_restored = False
        if paths.catalog.is_file():
            if manifest.get("preexisted", {}).get("catalog"):
                original = backup / paths.catalog.name
                if original.is_file():
                    atomic_write(paths.catalog, original.read_bytes())
                    catalog_restored = True
            else:
                paths.catalog.unlink(missing_ok=True)
                catalog_removed = True
        # Final sweep of the state dir (manifest, backups, handoff state).
        shutil.rmtree(paths.state_dir, ignore_errors=True)
    except Exception:
        restore_backup(paths, backup)
        raise
    removed_credential = remove_api_key() if remove_credential else False
    return result(
        "uninstalled",
        catalog_removed=catalog_removed,
        catalog_restored=catalog_restored,
        credential_removed=removed_credential,
        backup=str(backup),
    )


def doctor(paths: Paths, codex_bin: Optional[str]) -> Dict[str, Any]:
    status = static_status(paths, codex_bin)
    handoff_state: Dict[str, Any] = {"directory": str(paths.handoff_dir), "files": []}
    if paths.handoff_dir.is_dir():
        names = sorted(entry.name for entry in paths.handoff_dir.iterdir())
        handoff_state["files"] = names
        handoff_state["active_pending"] = any(name.endswith(".pending.json") for name in names)
    top_level_deepseek_provider = False
    if paths.config.is_file():
        top_level_deepseek_provider = toml_has_table(
            paths.config.read_text(encoding="utf-8"),
            f"model_providers.{PROVIDER}",
        )
    payload = {
        **status,
        "handoff_state": handoff_state,
        "top_level_deepseek_provider": top_level_deepseek_provider,
        "notes": [
            "Top-level config.toml is never modified by this router.",
            "The DeepSeek provider is declared inside each agent TOML.",
        ],
    }
    return payload


# ---------------------------------------------------------------------------
# Live smoke tests (dual oracle: thread metadata + marker)
# ---------------------------------------------------------------------------


def query_child_metadata(
    paths: Paths, child_id: str, deadline: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    candidates: List[Tuple[float, Path]] = []
    for state_db in paths.codex_home.glob("state_*.sqlite"):
        try:
            candidates.append((state_db.stat().st_mtime, state_db))
        except OSError:
            continue
    for _, state_db in sorted(candidates, reverse=True)[:MAX_STATE_DATABASES]:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        try:
            with sqlite3.connect(
                f"{state_db.resolve().as_uri()}?mode=ro", uri=True, timeout=0.05
            ) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
                }
                required = {"id", "model_provider", "model", "agent_role"}
                if not required.issubset(columns):
                    continue
                row = connection.execute(
                    "SELECT model_provider, model, agent_role FROM threads WHERE id = ?",
                    (child_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            continue
        if row:
            return {"model_provider": row[0], "model": row[1], "agent_role": row[2]}
    return None


def wait_for_child_metadata(
    paths: Paths,
    child_id: str,
    timeout_seconds: float = METADATA_WAIT_SECONDS,
    poll_interval: float = 0.2,
) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        metadata = query_child_metadata(paths, child_id, deadline)
        if metadata is not None:
            return metadata
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval, remaining))


def _smoke_env(
    paths: Paths, codex_home: Optional[Path] = None
) -> Dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home or paths.codex_home)
    # Windows/Linux agents use env_key. macOS agents use the Python Keychain
    # helper in their TOML and must not decrypt the key redundantly here.
    if platform_name() == "macos" and credential_backend() == "macos-keychain":
        return env
    try:
        key = read_credential_key()
    except ManagerError:
        key = None
    if key:
        env[API_KEY_ENV] = key
    return env


def _direct_codex_config_args(
    config: RouterConfig, spec: AgentSpec, model: str
) -> List[str]:
    """Return session-only overrides for a top-level DeepSeek Codex execution."""
    settings = [
        f"model_provider={_toml_string(PROVIDER)}",
        f"model={_toml_string(model)}",
        "model_context_window=1000000",
        f"developer_instructions={_toml_string(_AGENT_INSTRUCTIONS[spec.role].strip())}",
        f"model_providers.{PROVIDER}.name={_toml_string('DeepSeek')}",
        f"model_providers.{PROVIDER}.base_url={_toml_string(config.base_url)}",
        f"model_providers.{PROVIDER}.wire_api={_toml_string(WIRE_API)}",
    ]
    if platform_name() == "macos" and credential_backend() == "macos-keychain":
        settings.extend(
            (
                f"model_providers.{PROVIDER}.auth.command={_toml_string(sys.executable or 'python3')}",
                f"model_providers.{PROVIDER}.auth.args={_toml_string_array([str(Path(__file__).resolve()), '_credential-get'])}",
                f"model_providers.{PROVIDER}.auth.timeout_ms=5000",
                f"model_providers.{PROVIDER}.auth.refresh_interval_ms=0",
            )
        )
    else:
        settings.append(f"model_providers.{PROVIDER}.env_key={_toml_string(API_KEY_ENV)}")
    args: List[str] = []
    for setting in settings:
        args.extend(("-c", setting))
    return args


def _parse_direct_codex_events(stdout: str) -> Dict[str, Any]:
    """Parse the stable JSONL envelope emitted by ``codex exec --json``."""
    thread_id: Optional[str] = None
    message: Optional[str] = None
    usage: Dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        elif event.get("type") == "item.completed":
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                message = item["text"].strip()
        elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    return {"thread_id": thread_id, "message": message, "usage": usage}


def direct_codex_delegate(
    paths: Paths,
    codex_bin: str,
    role: str,
    assignment: str,
    *,
    workspace: Optional[str] = None,
    policy: Optional[str] = None,
    modality: str = Modality.TEXT_ONLY,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Run a bounded DeepSeek role as a top-level Codex execution.

    Codex 0.149 intentionally prevents an agent role from replacing the
    provider inherited from its parent. A top-level execution can still select
    an explicitly configured custom provider, so the Router uses that public
    boundary as its Desktop-compatible transport.
    """
    if role not in AGENT_SPECS:
        raise ManagerError("invalid_agent_type", f"Unsupported agent type: {role}")
    task = assignment.strip()
    if not task:
        raise ManagerError("invalid_assignment", "The delegated assignment is empty.")
    if len(task) > MAX_ASSIGNMENT_CHARS:
        raise ManagerError(
            "assignment_too_large",
            f"The delegated assignment exceeds {MAX_ASSIGNMENT_CHARS} characters.",
        )
    if not deepseek_allowed(modality):
        raise ManagerError(
            "vision_critical",
            "DeepSeek delegation requires text or a parent-supplied Visual Context Packet.",
        )
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ManagerError(
            "invalid_delegate_timeout", "Delegate timeout must be a positive integer."
        )
    resolved_policy = policy or ("FAST" if role == FLASH_ROLE else "REACT")
    try:
        reasoning = render_reasoning_context(role, resolved_policy)
    except RouteContractError as exc:
        raise ManagerError("invalid_policy", str(exc)) from exc

    resolved_timeout_seconds = (
        timeout_seconds
        if timeout_seconds is not None
        else (
            COMPLEX_PRO_TIMEOUT_SECONDS
            if role == PRO_ROLE
            and resolved_policy == "REACT"
            and re.search(r"(?m)^QUALITY CLOSURE\s*$", task)
            else DIRECT_CODEX_TIMEOUT_SECONDS
        )
    )

    target_workspace = Path(workspace or os.getcwd()).expanduser().resolve()
    if not target_workspace.is_dir():
        raise ManagerError(
            "workspace_missing", f"Delegation workspace is not a directory: {target_workspace}"
        )
    config = resolve_router_config(paths)
    spec = AGENT_SPECS[role]
    model = config.flash_model if role == FLASH_ROLE else config.pro_model
    prompt = (
        "BEGIN PARENT ASSIGNMENT\n"
        f"{task}\n"
        "END PARENT ASSIGNMENT\n\n"
        f"{reasoning}"
    )
    command = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--json",
        "-s",
        spec.sandbox_mode,
        "-C",
        str(target_workspace),
        *_direct_codex_config_args(config, spec, model),
        "-",
    ]
    # Keep direct executions independent from the parent Codex state database.
    # Codex Desktop may run tools under a read-only sandbox identity, while the
    # delegated top-level execution still needs a writable CODEX_HOME.
    with tempfile.TemporaryDirectory(
        prefix=f"codex-deepseek-router-{role}-"
    ) as runtime_home:
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                input=prompt,
                env=_smoke_env(paths, Path(runtime_home)),
                timeout=resolved_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ManagerError(
                "delegate_timeout",
                f"Direct Codex delegation for {role} timed out after {resolved_timeout_seconds} seconds.",
                {"timeout_seconds": resolved_timeout_seconds},
            ) from exc
    if proc.returncode != 0:
        raise ManagerError(
            "delegate_failed",
            f"Direct Codex delegation for {role} failed.",
            {"stderr": proc.stderr[-2000:]},
        )
    evidence = _parse_direct_codex_events(proc.stdout)
    if not evidence["message"]:
        raise ManagerError(
            "invalid_response",
            f"Direct Codex delegation for {role} returned no final agent message.",
            {"stderr": proc.stderr[-2000:]},
        )
    return result(
        "completed",
        transport_mode="direct_codex",
        role=role,
        model=model,
        model_provider=PROVIDER,
        agent_role=role,
        thread_id=evidence["thread_id"],
        message=evidence["message"],
        usage=evidence["usage"],
        timeout_seconds=resolved_timeout_seconds,
    )


def _stage_command(paths: Paths, role: str, expected_line: str) -> str:
    """The parent runs this exact command with the Bash tool before spawning."""
    if platform_name() == "windows":
        ps1 = paths.hooks_install_dir / "plaintext-handoff.ps1"
        return (
            f"printf '%s' 'Compute 17*23 and reply exactly this line: {expected_line} 391' | "
            f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-File "{ps1}" -Mode stage -AgentType {role} -Policy FAST '
            f'-Modality TEXT_ONLY -StateDirectory "{paths.handoff_dir}"'
        )
    handoff_script = paths.hooks_install_dir / "plaintext_handoff.py"
    return (
        f"printf '%s' 'Compute 17*23 and reply exactly this line: {expected_line} 391' | "
        f'python3 "{handoff_script}" --mode stage --agent-type {role} --policy FAST '
        f'--modality TEXT_ONLY --state-directory "{paths.handoff_dir}"'
    )


def _parse_native_smoke_events(stdout: str) -> Dict[str, Any]:
    """Extract routing evidence from the stable ``codex exec --json`` envelope.

    Multi-agent V2 wait events can be empty because completion is delivered via
    the agent mailbox. The smoke parent is instructed to echo that callback, so
    the final agent message is retained as the V2 response oracle.
    """
    parent_thread_id: Optional[str] = None
    child_ids: List[str] = []
    child_messages: Dict[str, str] = {}
    final_message: Optional[str] = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            parent_thread_id = event["thread_id"]
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"collab_tool_call", "collab_agent_tool_call"}:
            tool = item.get("tool")
            if tool in {"spawn_agent", "spawnAgent"}:
                for child_id in item.get("receiver_thread_ids") or item.get(
                    "receiverThreadIds"
                ) or []:
                    if isinstance(child_id, str) and child_id not in child_ids:
                        child_ids.append(child_id)
            if tool in {"wait", "wait_agent", "waitAgent"}:
                states = item.get("agents_states") or item.get("agentsStates") or {}
                if not isinstance(states, dict):
                    continue
                for receiver_id, state in states.items():
                    if not isinstance(receiver_id, str) or not isinstance(state, dict):
                        continue
                    message = state.get("message")
                    if state.get("status") == "completed" and isinstance(message, str):
                        child_messages[receiver_id] = message.strip()
        elif item_type == "agent_message" and isinstance(item.get("text"), str):
            final_message = item["text"].strip()
    return {
        "parent_thread_id": parent_thread_id,
        "child_ids": child_ids,
        "child_messages": child_messages,
        "final_message": final_message,
    }


def _recent_child_ids(
    paths: Paths,
    role: str,
    model: str,
    started_after_ms: int,
    parent_thread_id: Optional[str],
) -> List[str]:
    """Recover a V2 child id when this Codex build omits collab JSONL items.

    The query is deliberately strict and the caller still requires exactly one
    result. It is a second transport for the identifier, not weaker route proof:
    the marker and full provider/model/role metadata are validated afterwards.
    """
    child_ids: set[str] = set()
    candidates: List[Tuple[float, Path]] = []
    for state_db in paths.codex_home.glob("state_*.sqlite"):
        try:
            candidates.append((state_db.stat().st_mtime, state_db))
        except OSError:
            continue
    required = {"id", "created_at_ms", "model_provider", "model", "agent_role"}
    for _, state_db in sorted(candidates, reverse=True)[:MAX_STATE_DATABASES]:
        try:
            with sqlite3.connect(
                f"{state_db.resolve().as_uri()}?mode=ro", uri=True, timeout=0.05
            ) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
                }
                if not required.issubset(columns):
                    continue
                rows = connection.execute(
                    """SELECT id FROM threads
                       WHERE created_at_ms >= ?
                         AND model_provider = ?
                         AND model = ?
                         AND agent_role = ?""",
                    (started_after_ms, PROVIDER, model, role),
                ).fetchall()
        except (OSError, sqlite3.Error):
            continue
        for row in rows:
            child_id = row[0]
            if isinstance(child_id, str) and child_id != parent_thread_id:
                child_ids.add(child_id)
    return sorted(child_ids)


def native_spawn_smoke(paths: Paths, codex_bin: str, role: str, model: str) -> Dict[str, Any]:
    """Prove: Parent -> spawn_agent -> DeepSeek child -> callback, for one role."""
    parent_model = parent_config_snapshot(paths)["parent_model"]
    if not parent_model:
        raise ManagerError("parent_model_unconfigured", "config.toml has no explicit parent model.")
    marker = uuid.uuid4().hex
    expected_line = f"NATIVE_{role.upper()}_OK {marker}"
    stage_command = _stage_command(paths, role, expected_line)
    prompt = (
        f"Run this exact command with the Bash tool:\n{stage_command}\n"
        f"Then use the spawn_agent tool exactly once with agent_type {role} and fork_turns none.\n"
        "Then use the wait tool to wait for that subagent.\n"
        "Then reply with only the final response of the subagent."
    )
    env = _smoke_env(paths)
    started_after_ms = int(time.time() * 1000)
    try:
        proc = subprocess.run(
            [
                codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--json",
                "-s",
                "workspace-write",
                "-C",
                str(paths.codex_home),
                "-m",
                parent_model,
                prompt,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise ManagerError(
            "child_timeout",
            f"Smoke test for {role} timed out before the child returned.",
        ) from exc
    if proc.returncode != 0:
        stderr = proc.stderr[-1200:]
        combined_output = f"{proc.stderr}\n{proc.stdout}".lower()
        if "selected model is at capacity" in combined_output:
            raise ManagerError(
                "parent_model_capacity",
                "The configured parent model is currently at capacity; retry the Native smoke test later or select an available parent model.",
            )
        if "hook" in stderr.lower() and not hook_trusted(paths, codex_bin):
            raise ManagerError(
                "hook_untrusted",
                "The plaintext handoff hook has not been reviewed. Run /hooks in the interactive Codex CLI and trust the hook, then retry.",
                {"stderr": stderr},
            )
        raise ManagerError(
            "child_start_failed",
            f"Native spawn_agent smoke for {role} failed.",
            {"stderr": stderr},
        )

    evidence = _parse_native_smoke_events(proc.stdout)
    child_ids = evidence["child_ids"]
    if not child_ids:
        child_ids = _recent_child_ids(
            paths,
            role=role,
            model=model,
            started_after_ms=started_after_ms,
            parent_thread_id=evidence["parent_thread_id"],
        )

    child_id = child_ids[0] if len(child_ids) == 1 else None
    child_message = evidence["child_messages"].get(child_id) if child_id else None
    if child_id and not child_message:
        child_message = evidence["final_message"]
    metadata = wait_for_child_metadata(paths, child_id) if child_id else None
    expected = {"model_provider": PROVIDER, "model": model, "agent_role": role}
    marker_ok = bool(child_message) and expected_line in child_message and "391" in child_message
    if len(child_ids) != 1 or not marker_ok or metadata != expected:
        raise ManagerError(
            "native_route_mismatch",
            f"Native child routing evidence for {role} is incomplete or does not match the expected DeepSeek configuration.",
            {
                "child_ids": child_ids,
                "child_message": child_message,
                "metadata": metadata,
                "expected": expected,
                "expected_line": expected_line,
            },
        )
    return {
        "role": role,
        "model": model,
        "model_provider": PROVIDER,
        "agent_role": role,
        "marker_verified": True,
        "child_id": child_id,
    }


def direct_codex_smoke(
    paths: Paths, codex_bin: str, role: str, model: str
) -> Dict[str, Any]:
    """Prove a top-level DeepSeek Codex execution returns the expected marker."""
    marker = uuid.uuid4().hex
    expected_line = f"DIRECT_{role.upper()}_OK {marker}"
    delegated = direct_codex_delegate(
        paths,
        codex_bin,
        role,
        f"Compute 17*23 and reply exactly this line: {expected_line} 391",
        workspace=str(paths.codex_home),
        policy="FAST" if role == FLASH_ROLE else "REACT",
    )
    message = delegated["message"]
    if expected_line not in message or "391" not in message:
        raise ManagerError(
            "direct_route_mismatch",
            f"Direct Codex routing evidence for {role} does not match the expected marker.",
            {"message": message, "expected_line": expected_line},
        )
    return {
        "role": role,
        "model": model,
        "model_provider": PROVIDER,
        "agent_role": role,
        "transport_mode": TransportMode.DIRECT_CODEX.value,
        "marker_verified": True,
        "thread_id": delegated["thread_id"],
    }


def run_tests(paths: Paths, codex_bin: str) -> Dict[str, Any]:
    status = static_status(paths, codex_bin)
    if status["status"] not in {"configured", "ready"}:
        raise ManagerError("not_configured", "Static configuration is incomplete; live tests cannot run.", status)
    transport = TransportMode(status["transport_mode"])
    if transport != TransportMode.DIRECT_CODEX:
        if not plugin_hook_available(paths):
            raise ManagerError("plugin_hook_missing", "The Plugin Hook is not available; install or reload the Plugin before native smoke tests.")
        if not hook_trusted(paths, codex_bin):
            raise ManagerError(
                "hook_untrusted",
                "The Plugin Hook has not been reviewed yet. Codex normally shows its review UI; use /hooks in the interactive CLI only as a fallback, then run test again.",
            )
    config = resolve_router_config(paths)
    results: Dict[str, Any] = {}
    models = {FLASH_ROLE: config.flash_model, PRO_ROLE: config.pro_model}
    for role, model in models.items():
        if transport == TransportMode.DIRECT_CODEX:
            results[role] = direct_codex_smoke(paths, codex_bin, role, model)
        else:
            results[role] = native_spawn_smoke(paths, codex_bin, role, model)
    manifest = read_manifest(paths)
    manifest["last_test"] = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "transport_mode": transport.value,
        "flash": results[FLASH_ROLE],
        "pro": results[PRO_ROLE],
    }
    write_manifest(paths, manifest)
    return result(
        "ready",
        transport_mode=transport.value,
        flash=results[FLASH_ROLE],
        pro=results[PRO_ROLE],
        new_task_required=True,
        restart_required=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "status",
            "setup",
            "test",
            "repair",
            "migrate",
            "disable",
            "uninstall",
            "doctor",
            "delegate",
        ),
    )
    parser.add_argument("--codex-home")
    parser.add_argument("--api-key-stdin", action="store_true")
    parser.add_argument("--skip-live-test", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--flash-model")
    parser.add_argument("--pro-model")
    parser.add_argument("--agent-type", choices=sorted(VALID_AGENTS))
    parser.add_argument("--policy", choices=POLICIES)
    parser.add_argument("--modality", choices=tuple(Modality), default=Modality.TEXT_ONLY)
    parser.add_argument("--workspace")
    parser.add_argument(
        "--delegate-timeout",
        type=positive_integer,
        help="Positive timeout override in seconds for delegate; defaults to 900 or 1800 for Complex Pro + REACT.",
    )
    parser.add_argument("--remove-credential", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv == ["_credential-get"]:
        try:
            secret = read_credential_key()
            if not secret:
                print("DeepSeek API key is not available.", file=sys.stderr)
                return 2
            sys.stdout.write(secret)
            return 0
        except ManagerError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    paths = resolve_paths(args.codex_home)
    try:
        if args.command != "setup" and any(
            value is not None for value in (args.base_url, args.flash_model, args.pro_model)
        ):
            raise ManagerError(
                "invalid_configuration",
                "--base-url, --flash-model and --pro-model are only valid with setup.",
            )
        if args.command != "delegate" and args.delegate_timeout is not None:
            raise ManagerError(
                "invalid_configuration",
                "--delegate-timeout is only valid with delegate.",
            )
        codex_bin: Optional[str] = None
        if args.command in {"status", "setup", "repair", "test", "doctor", "delegate"}:
            try:
                codex_bin = find_desktop_codex()
            except ManagerError as exc:
                if args.command in {"setup", "repair", "test"}:
                    raise
                codex_bin = None  # status/doctor degrade gracefully
        if args.command == "status":
            payload = static_status(paths, codex_bin)
        elif args.command == "doctor":
            payload = doctor(paths, codex_bin)
        elif args.command == "delegate":
            if not args.agent_type:
                raise ManagerError(
                    "invalid_agent_type", "delegate requires --agent-type."
                )
            assignment = sys.stdin.read(MAX_ASSIGNMENT_CHARS + 1)
            payload = direct_codex_delegate(
                paths,
                codex_bin or "",
                args.agent_type,
                assignment,
                workspace=args.workspace,
                policy=args.policy,
                modality=args.modality,
                timeout_seconds=args.delegate_timeout,
            )
        else:
            with operation_lock(paths):
                if args.command == "setup":
                    payload = setup(
                        paths,
                        codex_bin or "",
                        args.api_key_stdin,
                        args.skip_live_test,
                        base_url=args.base_url,
                        flash_model=args.flash_model,
                        pro_model=args.pro_model,
                    )
                elif args.command == "repair":
                    payload = repair(paths, codex_bin or "")
                elif args.command == "test":
                    payload = run_tests(paths, codex_bin or "")
                elif args.command == "migrate":
                    payload = migrate_legacy(paths)
                elif args.command == "disable":
                    payload = disable(paths)
                else:
                    payload = uninstall(paths, args.remove_credential)
        emit(payload, args.json)
        return 0 if payload["status"] not in {"partial", "credential_missing"} else 2
    except ManagerError as exc:
        emit(manager_error_result(exc), args.json)
        return 2
    except subprocess.TimeoutExpired:
        emit(result("timeout", message="Operation timed out. No credential was printed."), args.json)
        return 3
    except Exception as exc:  # noqa: BLE001 - top-level safety net, never prints secrets
        emit(result("failed", message=f"{type(exc).__name__}: {exc}"), args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
