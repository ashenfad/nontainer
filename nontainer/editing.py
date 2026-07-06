"""Exact-string edit engine with agent-tolerant matching.

Ported from agex's ``agent/loop/file_editing.py`` (same author, same
battle scars — agex-ts carries the identical strategy set). The tool
contract stays "exact match"; these fallbacks absorb the two failure
modes real agents hit constantly without changing what agents are told:

1. exact match
2. trailing-whitespace-flexible (file has trailing spaces the agent
   didn't reproduce)
3. indent-flexible (agent quoted the block at a different indent
   baseline) — with the replacement re-indented to the file's baseline

Plus two ergonomics ported with it:

- "already applied": search absent but the replacement text present →
  idempotent retry, not an error
- on failure, a SequenceMatcher "did you mean these lines?" snippet
  with line numbers, so the error is a self-correction prompt

One deliberate fix over the source: regex substitution uses a callable
replacement, so replacement text containing ``\\1`` or backslashes is
inserted literally instead of being read as backreferences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher


class EditError(ValueError):
    """Actionable edit failure — the message is written for the agent."""


@dataclass(frozen=True)
class EditOutcome:
    content: str
    count: int
    mode: str  # "exact" | "trailing_ws" | "indent_flexible" | "already_applied"


def find_similar_lines(
    search: str, content: str, threshold: float = 0.6, context: int = 3
) -> str | None:
    """Best-matching chunk of ``content`` with line numbers, or None."""
    search_lines = search.splitlines()
    content_lines = content.splitlines()
    n = len(search_lines)
    if not search_lines or not content_lines or n > len(content_lines):
        return None

    best_ratio, best_start = 0.0, 0
    for i in range(len(content_lines) - n + 1):
        candidate = "\n".join(content_lines[i : i + n])
        ratio = SequenceMatcher(None, search, candidate).ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, i
    if best_ratio < threshold:
        return None

    ctx_start = max(0, best_start - context)
    ctx_end = min(len(content_lines), best_start + n + context)
    out = []
    for i in range(ctx_start, ctx_end):
        marker = ">" if best_start <= i < best_start + n else " "
        out.append(f"{marker} {i + 1:4d} | {content_lines[i]}")
    return "\n".join(out)


def _trailing_ws_pattern(search: str) -> re.Pattern:
    parts = [re.escape(line.rstrip()) + r"[ \t]*" for line in search.split("\n")]
    return re.compile("\n".join(parts))


def _indent_flexible_matches(search: str, content: str) -> list[tuple[int, int, str]]:
    """(start, end, matched_text) for structure-equal blocks at any indent."""
    search_lines = search.split("\n")
    content_lines = content.split("\n")

    anchor_stripped, anchor_idx = None, 0
    for idx, line in enumerate(search_lines):
        if line.strip():
            anchor_stripped, anchor_idx = line.strip(), idx
            break
    if anchor_stripped is None:
        return []

    search_stripped = [line.strip() for line in search_lines]
    matches = []
    for i, content_line in enumerate(content_lines):
        if content_line.strip() != anchor_stripped:
            continue
        start_line = i - anchor_idx
        end_line = start_line + len(search_lines)
        if start_line < 0 or end_line > len(content_lines):
            continue
        if all(
            search_stripped[j] == content_lines[start_line + j].strip()
            for j in range(len(search_stripped))
        ):
            start_pos = sum(len(content_lines[k]) + 1 for k in range(start_line))
            matched_text = "\n".join(content_lines[start_line:end_line])
            matches.append((start_pos, start_pos + len(matched_text), matched_text))
    return matches


def _reindent(replacement: str, search: str, matched_text: str) -> str:
    """Shift ``replacement``'s indent so it drops in where ``matched_text``
    was, using the file's indent character."""

    def base_indent(lines: list[str]) -> tuple[int, str]:
        for line in lines:
            stripped = line.lstrip()
            if stripped:
                leading = line[: len(line) - len(stripped)]
                char = "\t" if "\t" in leading else " "
                return leading.count("\t") * 4 + leading.count(" "), char
        return 0, " "

    search_indent, _ = base_indent(search.split("\n"))
    target_indent, target_char = base_indent(matched_text.split("\n"))
    repl_lines = replacement.split("\n")
    repl_indent, _ = base_indent(repl_lines)

    delta = target_indent - (
        search_indent if repl_indent == search_indent else repl_indent
    )

    adjusted = []
    for line in repl_lines:
        stripped = line.lstrip()
        if not stripped:
            adjusted.append("")
            continue
        current = line[: len(line) - len(stripped)]
        new_indent = max(0, current.count("\t") * 4 + current.count(" ") + delta)
        if target_char == "\t":
            leading = "\t" * (new_indent // 4) + " " * (new_indent % 4)
        else:
            leading = " " * new_indent
        adjusted.append(leading + stripped)
    return "\n".join(adjusted)


def apply_edit(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    path: str = "<file>",
) -> EditOutcome:
    """Tiered exact-ish replacement. Raises :class:`EditError` with an
    agent-actionable message on no-match or ambiguity."""
    if old_string == new_string:
        raise EditError("old_string and new_string are identical")

    # 1. exact
    count = content.count(old_string)
    if count:
        if count > 1 and not replace_all:
            raise EditError(
                f"old_string appears {count} times in {path}; include more "
                "surrounding context to make it unique, or pass replace_all"
            )
        limit = -1 if replace_all else 1
        return EditOutcome(
            content.replace(old_string, new_string, limit),
            count if replace_all else 1,
            "exact",
        )

    # 2. trailing-whitespace flexible
    pattern = _trailing_ws_pattern(old_string)
    ws_matches = list(pattern.finditer(content))
    if ws_matches:
        if len(ws_matches) > 1 and not replace_all:
            raise EditError(
                f"old_string matches {len(ws_matches)} places in {path} "
                "(ignoring trailing whitespace); add context or replace_all"
            )
        n = len(ws_matches) if replace_all else 1
        # callable replacement: never interpret backslashes/backrefs
        new_content = pattern.sub(lambda m: new_string, content, count=0 if replace_all else 1)
        return EditOutcome(new_content, n, "trailing_ws")

    # 3. indent-flexible
    indent_matches = _indent_flexible_matches(old_string, content)
    if indent_matches:
        if len(indent_matches) > 1 and not replace_all:
            raise EditError(
                f"old_string matches {len(indent_matches)} places in {path} "
                "(at different indentation); add context or replace_all"
            )
        to_apply = indent_matches if replace_all else indent_matches[:1]
        new_content = content
        for start, end, matched in reversed(to_apply):
            new_content = (
                new_content[:start]
                + _reindent(new_string, old_string, matched)
                + new_content[end:]
            )
        return EditOutcome(new_content, len(to_apply), "indent_flexible")

    # 4. already applied? (idempotent agent retry)
    if new_string in content:
        return EditOutcome(content, 0, "already_applied")

    # 5. actionable failure
    parts = [f"old_string not found in {path}."]
    similar = find_similar_lines(old_string, content)
    if similar:
        parts.append("Did you mean to match these lines?\n" + similar)
    else:
        preview = old_string[:200] + ("..." if len(old_string) > 200 else "")
        parts.append(f"Search was:\n{preview}")
    raise EditError("\n\n".join(parts))
