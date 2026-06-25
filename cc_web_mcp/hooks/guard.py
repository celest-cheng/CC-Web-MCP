from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cc_web_mcp.config import resolve_config_path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = resolve_config_path()
DEFAULT_STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClaudeCode" / "cc_web_model_state.json"
MODEL_ENV_NAMES = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_BASE_URL",
)
CC_WEB_TOOL_PREFIXES = ("mcp__cc-web__", "mcp__cc_web__")
CC_WEB_FETCH_TOOLS = ("mcp__cc-web__fetch_url", "mcp__cc_web__fetch_url")
NATIVE_WEB_TOOLS = {"WebFetch"}


def load_allowed_patterns(path: Path) -> list[str]:
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            patterns = raw.get("allowed_model_patterns", ["deepseek"])
            if isinstance(patterns, list):
                cleaned = [str(item).strip().lower() for item in patterns if str(item).strip()]
                return cleaned or ["deepseek"]
    except Exception:
        pass
    return ["deepseek"]


def load_config(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def allow_fetch_url_for_claude(path: Path) -> bool:
    return bool(load_config(path).get("allow_fetch_url_for_claude", False))


def block_native_web_for_allowed_models(path: Path) -> bool:
    return bool(load_config(path).get("block_native_web_for_allowed_models", True))


def model_matches_patterns(model: str | None, patterns: list[str]) -> bool:
    normalized = (model or "").lower()
    return any(pattern in normalized for pattern in patterns)


def is_claude_model(model: str | None) -> bool:
    normalized = (model or "").lower()
    return "claude" in normalized or normalized in {"opus", "sonnet", "haiku"}


def is_allowed_environment(patterns: list[str]) -> bool:
    return any(model_matches_patterns(os.environ.get(name), patterns) for name in MODEL_ENV_NAMES)


def has_claude_environment_model() -> bool:
    return any(is_claude_model(os.environ.get(name)) for name in MODEL_ENV_NAMES)


def deny(reason: str, additional_context: str | None = None) -> int:
    hook_output = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    if additional_context:
        hook_output["additionalContext"] = additional_context
    response = {
        "hookSpecificOutput": hook_output,
    }
    print(json.dumps(response, ensure_ascii=False))
    return 0


def load_state(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def is_cc_web_mcp_running() -> bool:
    """Best-effort check: is the cc-web MCP server process alive right now?

    Returns True when the process is found, or when we can't determine
    (safe default: assume available so existing blocking behaviour is preserved).
    """
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                [
                    "wmic",
                    "process",
                    "where",
                    "name='python.exe'",
                    "get",
                    "commandline",
                    "/format:csv",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                encoding="cp936",
            )
            # Exclude guard's own process (hooks.guard/hook-guard) from match
            for line in result.stdout.splitlines():
                if "cc_web_mcp" in line and "hooks.guard" not in line and "hook-guard" not in line:
                    return True
            return False
        else:
            result = subprocess.run(
                ["pgrep", "-f", "cc_web_mcp"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
    except Exception:
        return True  # can't probe → assume available (preserve existing behaviour)


def record_session_start(payload: dict[str, Any], state_path: Path) -> None:
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    state = load_state(state_path)
    state[session_id] = {
        "model": str(payload.get("model") or ""),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cwd": str(payload.get("cwd") or ""),
    }
    # ------------------------------------------------------------------
    # Probe whether the cc-web MCP server is actually alive *this* session.
    # When it isn't, the PreToolUse guard will allow native WebFetch as a
    # fallback instead of blocking it with no alternative available.
    # ------------------------------------------------------------------
    state["__mcp_status__"] = {
        "cc_web_available": is_cc_web_mcp_running(),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_state(state_path, state)


def guard_pre_tool_use(payload: dict[str, Any], state_path: Path, config_path: Path) -> int:
    tool_name = str(payload.get("tool_name") or "")
    is_cc_web_tool = tool_name.startswith(CC_WEB_TOOL_PREFIXES)
    is_native_web_tool = tool_name in NATIVE_WEB_TOOLS
    if not is_cc_web_tool and not is_native_web_tool:
        return 0

    patterns = load_allowed_patterns(config_path)
    session_id = str(payload.get("session_id") or "")
    state = load_state(state_path)
    model = str(state.get(session_id, {}).get("model") or "")

    if is_native_web_tool and block_native_web_for_allowed_models(config_path):
        if model:
            is_allowed_model = model_matches_patterns(model, patterns)
        else:
            is_allowed_model = is_allowed_environment(patterns)
        if is_allowed_model:
            # ── guard rail: only block native WebFetch when cc-web MCP is
            #    *actually* loaded this session.  When the MCP server failed to
            #    start (or wasn't picked up after a config change), allowing
            #    WebFetch is better than having no web access at all.
            mcp_status = state.get("__mcp_status__", {})
            cc_web_available = mcp_status.get("cc_web_available", True)
            if not cc_web_available:
                # cc-web MCP is NOT running — let WebFetch through as fallback
                return 0

            reason = (
                f"{tool_name} is disabled for this third-party model. "
                "Use cc-web MCP instead. Search with mcp__cc-web__research_brief "
                "or mcp__cc-web__web_search; fetch page content with "
                "mcp__cc-web__fetch_url."
            )
            additional_context = (
                f"Tool routing instruction: Do not retry {tool_name}. "
                "The current model matches cc-web allowed_model_patterns, so native "
                "Claude Code WebFetch is unavailable or unsuitable. "
                "For web research, call mcp__cc-web__research_brief first. "
                "If you need raw search results, call mcp__cc-web__web_search. "
                "If you need to read a specific URL, call mcp__cc-web__fetch_url. "
                "Continue the task by using cc-web MCP now."
            )
            return deny(reason, additional_context)
        return 0

    allow_claude_fetch = allow_fetch_url_for_claude(config_path)
    if model:
        if model_matches_patterns(model, patterns):
            return 0
        if tool_name in CC_WEB_FETCH_TOOLS and is_claude_model(model) and allow_claude_fetch:
            return 0
    else:
        if is_allowed_environment(patterns):
            return 0
        if tool_name in CC_WEB_FETCH_TOOLS and has_claude_environment_model() and allow_claude_fetch:
            return 0

    reason = (
        "cc-web MCP is only enabled for configured model patterns. "
        f"Allowed model keywords: {', '.join(patterns)}. "
        "Official Claude should use native WebSearch/WebFetch. "
        "To allow Claude to use cc-web fetch_url, set allow_fetch_url_for_claude: true."
    )
    additional_context = (
        "Tool routing instruction: Do not retry this cc-web MCP tool in the current "
        "model unless the user explicitly asks for cc-web or the configuration allows it. "
        "Official Claude should use native WebSearch/WebFetch for web access."
    )
    return deny(reason, additional_context)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args(argv)
    state_path = Path(args.state)
    config_path = Path(args.config)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return 0

    event_name = payload.get("hook_event_name")
    if event_name == "SessionStart":
        record_session_start(payload, state_path)
    elif event_name == "PreToolUse":
        return guard_pre_tool_use(payload, state_path, config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
