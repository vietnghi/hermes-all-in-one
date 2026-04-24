#!/usr/bin/env python3
"""
Post-sync patch: promote hermes-agent's DEFAULT_CODEX_MODELS into the
hermes-webui model lists so the UI stays in sync with the CLI.

Idempotent — safe to run multiple times.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
AGENT_CODEX = ROOT / "vendor/hermes-agent/hermes_cli/codex_models.py"
WEBUI_CONFIG = ROOT / "vendor/hermes-webui/api/config.py"


def _label(model_id: str) -> str:
    """gpt-5.5 → GPT-5.5,  gpt-5.3-codex → GPT-5.3 Codex, etc."""
    parts = model_id.split("-")
    out = []
    for p in parts:
        if p.lower() in {"gpt", "codex", "mini", "max", "nano", "pro", "chat", "spark"}:
            out.append(p.upper() if p.lower() == "gpt" else p.capitalize())
        else:
            out.append(p)
    # Rebuild: GPT-5.5, GPT-5.3 Codex, GPT-5.4 Mini …
    # First token is always "GPT", glue it to the version with a dash
    if out and out[0] == "GPT":
        head = "GPT-" + out[1] if len(out) > 1 else "GPT"
        tail = " ".join(out[2:])
        return (head + " " + tail).strip()
    return " ".join(out)


def _load_agent_models() -> list[str]:
    src = AGENT_CODEX.read_text(encoding="utf-8")
    m = re.search(
        r'DEFAULT_CODEX_MODELS\s*:\s*List\[str\]\s*=\s*\[(.*?)\]',
        src, re.DOTALL,
    )
    if not m:
        sys.exit(f"[patch] Could not parse DEFAULT_CODEX_MODELS from {AGENT_CODEX}")
    return re.findall(r'"([^"]+)"', m.group(1))


def _insert_before_first_match(text: str, anchor_pattern: str, new_line: str) -> str:
    """Insert new_line before the first line matching anchor_pattern, if not already present."""
    if new_line.strip() in text:
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.search(anchor_pattern, line):
            lines.insert(i, new_line + "\n")
            return "".join(lines)
    return text


def patch(text: str, models: list[str]) -> str:
    for model_id in models:
        lbl = _label(model_id)

        # 1. _FALLBACK_MODELS OpenAI block
        #    Insert before first existing openai/gpt line
        fallback_line = (
            f'    {{"provider": "OpenAI",    '
            f'"id": "openai/{model_id}",{" " * max(1, 32 - len(model_id))}'
            f'"label": "{lbl}"}},'
        )
        text = _insert_before_first_match(
            text,
            r'"provider":\s*"OpenAI"',
            fallback_line,
        )

        # 2. _PROVIDER_MODELS["openai"] block
        #    Insert before first {"id": "gpt- line inside that block
        openai_line = f'        {{"id": "{model_id}", "label": "{lbl}"}},'
        # Scope to the "openai": [ block (not openai-codex)
        def _patch_openai_block(t: str) -> str:
            block_re = re.compile(
                r'("openai":\s*\[)(.*?)(\s*\],)',
                re.DOTALL,
            )
            def replacer(m: re.Match) -> str:
                body = m.group(2)
                if model_id in body:
                    return m.group(0)
                # Insert before first entry
                first = re.search(r'\n\s*\{', body)
                if first:
                    pos = first.start()
                    body = body[:pos] + f"\n        {{\"id\": \"{model_id}\", \"label\": \"{lbl}\"}}," + body[pos:]
                return m.group(1) + body + m.group(3)
            return block_re.sub(replacer, t, count=1)
        text = _patch_openai_block(text)

        # 3. _PROVIDER_MODELS["openai-codex"] block
        def _patch_codex_block(t: str) -> str:
            block_re = re.compile(
                r'("openai-codex":\s*\[)(.*?)(\s*\],)',
                re.DOTALL,
            )
            def replacer(m: re.Match) -> str:
                body = m.group(2)
                if model_id in body:
                    return m.group(0)
                first = re.search(r'\n\s*\{', body)
                if first:
                    pos = first.start()
                    body = body[:pos] + f"\n        {{\"id\": \"{model_id}\", \"label\": \"{lbl}\"}}," + body[pos:]
                return m.group(1) + body + m.group(3)
            return block_re.sub(replacer, t, count=1)
        text = _patch_codex_block(text)

    return text


def main() -> None:
    if not AGENT_CODEX.exists():
        sys.exit(f"[patch] Not found: {AGENT_CODEX}")
    if not WEBUI_CONFIG.exists():
        sys.exit(f"[patch] Not found: {WEBUI_CONFIG}")

    models = _load_agent_models()
    print(f"[patch] Agent models: {models}")

    original = WEBUI_CONFIG.read_text(encoding="utf-8")
    patched = patch(original, models)

    if patched == original:
        print("[patch] webui config already up to date.")
        return

    WEBUI_CONFIG.write_text(patched, encoding="utf-8")
    print(f"[patch] Updated {WEBUI_CONFIG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
