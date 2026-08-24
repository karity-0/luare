from __future__ import annotations

import argparse
import hashlib
import html
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

CONTROL_WORDS = {"if", "then", "elseif", "else", "end", "while", "do", "for", "repeat", "until", "function", "local"}
CALL_EXCLUDE = {"if", "elseif", "while", "for", "function", "return", "type", "assert", "not", "and", "or"}

@dataclass
class Statement:
    text: str
    src_line: int
    sid: str = ""
    kind: str = "stmt"
    annotations: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)  # (symbol, id)


def stable_hex(text: str, prefix: str = "", n: int = 6) -> str:
    h = hashlib.blake2s(text.encode("utf-8"), digest_size=8).hexdigest().upper()
    return f"{prefix}0x{h[:n]}"


def strip_line_comment(s: str) -> tuple[str, str]:
    """Split Lua -- comment, respecting quoted strings and long brackets."""
    quote = None
    esc = False
    long_eq = None
    i = 0
    while i < len(s):
        ch = s[i]
        if long_eq is not None:
            close = "]" + "=" * long_eq + "]"
            j = s.find(close, i)
            if j == -1:
                return s, ""
            i = j + len(close)
            long_eq = None
            continue
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "[":
            m = re.match(r"\[(=*)\[", s[i:])
            if m:
                long_eq = len(m.group(1))
                i += len(m.group(0))
                continue
        if s.startswith("--", i):
            return s[:i].rstrip(), s[i:]
        i += 1
    return s.rstrip(), ""


def split_semicolons(line: str) -> list[str]:
    """Split semicolons only at expression nesting depth zero."""
    parts, cur = [], []
    quote = None
    esc = False
    long_eq = None
    depths = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    i = 0
    while i < len(line):
        ch = line[i]
        if long_eq is not None:
            close = "]" + "=" * long_eq + "]"
            if line.startswith(close, i):
                cur.extend(close)
                i += len(close)
                long_eq = None
                continue
            cur.append(ch); i += 1; continue
        if quote:
            cur.append(ch)
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == quote: quote = None
            i += 1; continue
        if ch in "'\"":
            quote = ch; cur.append(ch); i += 1; continue
        if ch == "[":
            m = re.match(r"\[(=*)\[", line[i:])
            if m:
                long_eq = len(m.group(1)); cur.extend(m.group(0)); i += len(m.group(0)); continue
        if ch in depths:
            depths[ch] += 1
        elif ch in pairs:
            k = pairs[ch]; depths[k] = max(0, depths[k] - 1)
        if ch == ";" and not any(depths.values()):
            p = "".join(cur).strip()
            if p: parts.append(p)
            cur = []
        else:
            cur.append(ch)
        i += 1
    p = "".join(cur).strip()
    if p: parts.append(p)
    return parts


def split_top_level_commas(text: str) -> list[str]:
    """Split a Lua expression list without cutting nested/string commas."""
    return [part["text"] for part in split_top_level_comma_spans(text)]


def split_top_level_comma_spans(text: str) -> list[dict]:
    """Split a Lua expression list and preserve source spans."""
    parts: list[str] = []
    start = 0
    quote = None
    esc = False
    long_eq = None
    depths = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    i = 0
    while i < len(text):
        ch = text[i]
        if long_eq is not None:
            close = "]" + "=" * long_eq + "]"
            if text.startswith(close, i):
                i += len(close)
                long_eq = None
                continue
            i += 1
            continue
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "[":
            m = re.match(r"\[(=*)\[", text[i:])
            if m:
                long_eq = len(m.group(1))
                i += len(m.group(0))
                continue
        if ch in depths:
            depths[ch] += 1
        elif ch in pairs:
            key = pairs[ch]
            depths[key] = max(0, depths[key] - 1)
        elif ch == "," and not any(depths.values()):
            raw = text[start:i]
            part = raw.strip()
            if part:
                left = len(raw) - len(raw.lstrip())
                right = len(raw.rstrip())
                parts.append({"text": part, "start": start + left, "end": start + right})
            start = i + 1
        i += 1
    raw = text[start:]
    tail = raw.strip()
    if tail:
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        parts.append({"text": tail, "start": start + left, "end": start + right})
    return parts


def find_top_level_assignment(text: str) -> int | None:
    """Find plain assignment `=` while excluding comparisons and nesting."""
    quote = None
    esc = False
    long_eq = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if long_eq is not None:
            close = "]" + "=" * long_eq + "]"
            if text.startswith(close, i):
                i += len(close)
                long_eq = None
                continue
            i += 1
            continue
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "[":
            m = re.match(r"\[(=*)\[", text[i:])
            if m:
                long_eq = len(m.group(1))
                i += len(m.group(0))
                continue
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0:
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if prev not in "<>=~" and nxt != "=":
                return i
        i += 1
    return None


def extract_value_expressions(text: str, kind: str) -> tuple[str, list[str]] | None:
    """Extract value-list evaluation units for assignments and returns."""
    stripped = text.strip()
    if kind == "terminal" and re.match(r"^return\b", stripped):
        rest = re.sub(r"^return\b", "", stripped, count=1).strip()
        return ("return", split_top_level_commas(rest)) if rest else None
    if kind != "stmt":
        return None
    eq = find_top_level_assignment(stripped)
    if eq is None:
        return None
    rhs = stripped[eq + 1:].strip()
    if not rhs:
        return None
    return "assignment", split_top_level_commas(rhs)


def extract_value_units(text: str, kind: str) -> tuple[str, list[dict]] | None:
    """Return assignment/return value expressions with statement-local spans.

    Lua adjusts every non-final expression in a value list to one result. The
    final expression is the only position that may expand to multiple results.
    Evaluation order between sibling expressions is intentionally not claimed.
    """
    stripped = text.strip()
    leading = len(text) - len(text.lstrip())
    if kind == "terminal" and re.match(r"^return\b", stripped):
        m = re.match(r"^return\b", stripped)
        assert m is not None
        payload_start = leading + m.end()
        while payload_start < len(text) and text[payload_start].isspace():
            payload_start += 1
        if payload_start >= len(text):
            return None
        units = split_top_level_comma_spans(text[payload_start:])
        for unit in units:
            unit["start"] += payload_start
            unit["end"] += payload_start
        return "return", units
    if kind != "stmt":
        return None
    eq = find_top_level_assignment(stripped)
    if eq is None:
        return None
    eq += leading
    payload_start = eq + 1
    while payload_start < len(text) and text[payload_start].isspace():
        payload_start += 1
    if payload_start >= len(text):
        return None
    units = split_top_level_comma_spans(text[payload_start:])
    for unit in units:
        unit["start"] += payload_start
        unit["end"] += payload_start
    return "assignment", units


def split_inline_controls(s: str) -> list[str]:
    """Best-effort split of compact one-line control flow into logical statements."""
    # Protect strings by scanning words outside strings/brackets, then cut at structural keywords.
    cuts = []
    quote = None; esc = False; depth = 0; long_eq = None; i = 0
    word_re = re.compile(r"\b(then|elseif|else|end|do|until)\b")
    while i < len(s):
        ch = s[i]
        if long_eq is not None:
            close = "]" + "=" * long_eq + "]"
            if s.startswith(close, i): i += len(close); long_eq = None; continue
            i += 1; continue
        if quote:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == quote: quote = None
            i += 1; continue
        if ch in "'\"": quote = ch; i += 1; continue
        if ch == "[":
            m = re.match(r"\[(=*)\[", s[i:])
            if m: long_eq = len(m.group(1)); i += len(m.group(0)); continue
        if ch in "({[": depth += 1
        elif ch in ")}]": depth = max(0, depth - 1)
        if depth == 0:
            m = word_re.match(s, i)
            if m:
                kw = m.group(1)
                # 'then'/'do' stay attached to header; split after them. others split before.
                if kw in ("then", "do"):
                    cuts.append((m.end(), "after"))
                else:
                    cuts.append((m.start(), "before"))
                    if kw in ("else", "end"):
                        cuts.append((m.end(), "after"))
                i = m.end(); continue
        i += 1
    if not cuts: return [s.strip()] if s.strip() else []
    points = sorted(set([0, len(s)] + [p for p, _ in cuts]))
    out = []
    for a, b in zip(points, points[1:]):
        x = s[a:b].strip()
        if x: out.append(x)
    return out


def normalize_source(src: str) -> list[Statement]:
    out: list[Statement] = []
    pending = ""
    pending_line = 1
    bracket_depth = 0
    quote = None
    long_eq = None

    # Conservative multiline joining based on bracket balance and trailing operators.
    for lineno, raw in enumerate(src.splitlines(), 1):
        code, comment = strip_line_comment(raw)
        stripped = code.strip()
        if not stripped:
            if comment.strip():
                out.append(Statement(comment.strip(), lineno, kind="comment"))
            continue
        text = (pending + " " + stripped).strip() if pending else stripped
        # rough balance outside strings for multiline calls/tables
        bal = 0; q = None; esc = False
        for ch in text:
            if q:
                if esc: esc = False
                elif ch == "\\": esc = True
                elif ch == q: q = None
            elif ch in "'\"": q = ch
            elif ch in "({[": bal += 1
            elif ch in ")}]": bal -= 1
        trailing = bool(re.search(r"(?:[,+\-*/%^..]|\band\b|\bor\b|=)\s*$", text))
        if bal > 0 or trailing:
            if not pending: pending_line = lineno
            pending = text
            continue
        for semi in split_semicolons(text):
            for part in split_inline_controls(semi):
                out.append(Statement(part, pending_line if pending else lineno))
        pending = ""
    if pending:
        out.append(Statement(pending, pending_line))
    return out


def classify(s: str) -> str:
    t = s.strip()
    if t.startswith("--"): return "comment"
    if re.match(r"^::[A-Za-z_]\w*::$", t): return "label"
    if re.match(r"^(local\s+)?function\b", t): return "function"
    if re.match(r"^(?:local\s+)?[A-Za-z_]\w*(?:(?:\.|:)[A-Za-z_]\w*)*\s*=\s*function\s*\(", t): return "function"
    if re.match(r"^if\b.*\bthen\s*$", t): return "if"
    if re.match(r"^elseif\b.*\bthen\s*$", t): return "elseif"
    if t == "else": return "else"
    if t == "end": return "end"
    if re.match(r"^while\b.*\bdo\s*$", t): return "while"
    if re.match(r"^for\b.*\bdo\s*$", t): return "for"
    if t == "repeat": return "repeat"
    if re.match(r"^until\b", t): return "until"
    if t == "do": return "do"
    if re.match(r"^(break|return\b|goto\b)", t): return "terminal"
    return "stmt"


def extract_label_name(text: str) -> str | None:
    m = re.match(r"^::([A-Za-z_]\w*)::$", text.strip())
    return m.group(1) if m else None


def extract_goto_name(text: str) -> str | None:
    m = re.match(r"^goto\s+([A-Za-z_]\w*)\s*$", text.strip())
    return m.group(1) if m else None


def condition_expr(text: str, kind: str) -> str | None:
    t = text.strip()
    patterns = {
        "if": r"^if\s+(.*?)\s+then$",
        "elseif": r"^elseif\s+(.*?)\s+then$",
        "while": r"^while\s+(.*?)\s+do$",
        "until": r"^until\s+(.*)$",
    }
    pat = patterns.get(kind)
    if not pat:
        return None
    m = re.match(pat, t)
    return m.group(1).strip() if m else None


def split_short_circuit(expr: str) -> tuple[list[str], list[str]]:
    """Split top-level Lua `and`/`or` while preserving strings and nesting."""
    parts: list[str] = []
    ops: list[str] = []
    start = 0
    quote = None
    esc = False
    long_eq = None
    depth = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if long_eq is not None:
            close = "]" + "=" * long_eq + "]"
            if expr.startswith(close, i):
                i += len(close); long_eq = None; continue
            i += 1; continue
        if quote:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == quote: quote = None
            i += 1; continue
        if ch in "'\"":
            quote = ch; i += 1; continue
        if ch == "[":
            m = re.match(r"\[(=*)\[", expr[i:])
            if m:
                long_eq = len(m.group(1)); i += len(m.group(0)); continue
        if ch in "({[": depth += 1
        elif ch in ")}]": depth = max(0, depth - 1)
        if depth == 0:
            m = re.match(r"\b(and|or)\b", expr[i:])
            if m:
                part = expr[start:i].strip()
                if part:
                    parts.append(part)
                    ops.append(m.group(1))
                    i += len(m.group(0))
                    start = i
                    continue
        i += 1
    tail = expr[start:].strip()
    if tail:
        parts.append(tail)
    if len(parts) != len(ops) + 1:
        return [expr], []
    return parts, ops


def short_circuit_route(ops: list[str], step: int, true_target: str, false_target: str) -> str:
    """Describe Lua boolean evaluation for a 1-based operand step.

    Lua gives `and` higher precedence than `or`. For an `and` operand that
    becomes false, evaluation skips the rest of that AND-chain and resumes at
    the operand after the next OR, if one exists.
    """
    op_index = step - 1
    if op_index >= len(ops):
        return f"true -> {true_target} | false -> {false_target}"
    op = ops[op_index]
    if op == "or":
        return f"true -> {true_target} | false -> <expr:{step+1}>"
    next_or = next((j for j in range(op_index, len(ops)) if ops[j] == "or"), None)
    false_route = f"<expr:{next_or + 2}>" if next_or is not None else false_target
    return f"true -> <expr:{step+1}> | false -> {false_route}"


def resolve_expr_routes(route: str, step_ids: list[str]) -> str:
    """Replace temporary <expr:N> routes with concrete E anchors."""
    def repl(m: re.Match) -> str:
        step = int(m.group(1))
        if 1 <= step <= len(step_ids):
            return step_ids[step - 1]
        return m.group(0)
    return re.sub(r"<expr:(\d+)>", repl, route)


def extract_function_name(text: str) -> str | None:
    m = re.match(r"^(?:local\s+)?function\s+([A-Za-z_][\w.:]*)\s*\(", text.strip())
    if m:
        return m.group(1)
    m = re.match(
        r"^(?:local\s+)?([A-Za-z_]\w*(?:(?:\.|:)[A-Za-z_]\w*)*)\s*=\s*function\s*\(",
        text.strip(),
    )
    return m.group(1) if m else None


def extract_calls(text: str) -> list[str]:
    if classify(text) == "function":
        # Strip declaration head, but preserve possible weird body not expected after normalization.
        return []
    names = []
    # foo(...), obj.foo(...), obj:foo(...)
    for m in re.finditer(r"(?<![\w])([A-Za-z_]\w*(?:(?:\.|:)[A-Za-z_]\w*)*)\s*\(", text):
        name = m.group(1)
        if name.split(".")[0].split(":")[0] not in CALL_EXCLUDE:
            names.append(name)
    return list(dict.fromkeys(names))


def extract_call_sites(text: str) -> list[dict]:
    """Return nested call sites with parent/child relationships.

    Duplicate calls are preserved. The structure records only semantic
    dependencies that are safe to claim: a nested child call must complete
    before its containing parent call can be invoked.
    """
    if classify(text) == "function":
        return []

    candidates = []
    call_re = re.compile(r"(?<![\w])([A-Za-z_]\w*(?:(?:\.|:)[A-Za-z_]\w*)*)\s*\(")
    for m in call_re.finditer(text):
        name = m.group(1)
        if name.split(".")[0].split(":")[0] in CALL_EXCLUDE:
            continue
        open_i = text.find("(", m.start(1) + len(name))
        if open_i >= 0:
            candidates.append({"name": name, "start": m.start(1), "open": open_i})

    for site in candidates:
        depth = 0
        quote = None
        esc = False
        long_eq = None
        close_i = None
        i = site["open"]
        while i < len(text):
            ch = text[i]
            if long_eq is not None:
                close = "]" + "=" * long_eq + "]"
                if text.startswith(close, i):
                    i += len(close)
                    long_eq = None
                    continue
                i += 1
                continue
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = None
                i += 1
                continue
            if ch in "'\"":
                quote = ch
                i += 1
                continue
            if ch == "[":
                lm = re.match(r"\[(=*)\[", text[i:])
                if lm:
                    long_eq = len(lm.group(1))
                    i += len(lm.group(0))
                    continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_i = i
                    break
            i += 1
        site["end"] = close_i if close_i is not None else len(text) - 1

    for site in candidates:
        containers = [
            other for other in candidates
            if other is not site and other["start"] < site["start"] and other["end"] >= site["end"]
        ]
        site["parent"] = min(containers, key=lambda x: x["end"] - x["start"]) if containers else None

    for site in candidates:
        site["children"] = [x for x in candidates if x.get("parent") is site]
        site["children"].sort(key=lambda x: x["start"])
    return candidates


def match_blocks(stmts: list[Statement]) -> tuple[dict[int, dict], dict[int, int]]:
    """Match structural blocks before statement IDs are assigned."""
    stack: list[dict] = []
    block_meta: dict[int, dict] = {}
    end_to_start: dict[int, int] = {}
    for i, st in enumerate(stmts):
        kind = st.kind
        if kind in ("if", "while", "for", "function", "do", "repeat"):
            stack.append({"kind": kind, "start": i, "arms": [i] if kind == "if" else []})
        elif kind in ("elseif", "else"):
            for frame in reversed(stack):
                if frame["kind"] == "if":
                    frame["arms"].append(i)
                    break
        elif kind == "until":
            repeat_pos = next(
                (p for p in range(len(stack) - 1, -1, -1) if stack[p]["kind"] == "repeat"),
                None,
            )
            if repeat_pos is not None:
                frame = stack.pop(repeat_pos)
                frame["end"] = i
                block_meta[frame["start"]] = frame
                end_to_start[i] = frame["start"]
        elif kind == "end" and stack:
            frame = stack.pop()
            frame["end"] = i
            block_meta[frame["start"]] = frame
            end_to_start[i] = frame["start"]
    return block_meta, end_to_start


def assign_runtime_statement_ids(stmts: list[Statement], block_meta: dict[int, dict]) -> None:
    """Assign IDs in deterministic runtime-first visitation order.

    Function declarations execute in their surrounding scope, but their bodies
    do not. A known function body is therefore first visited at its first call;
    bodies that cannot be reached statically are appended in source order so
    every logical statement still receives an anchor. Branch alternatives and
    unspecified sibling evaluations retain source order as a deterministic
    tie-breaker.
    """
    function_starts = {
        name: i
        for i, st in enumerate(stmts)
        if (name := extract_function_name(st.text)) is not None
    }
    order: list[int] = []
    visited: set[int] = set()
    walked_functions: set[int] = set()
    active_functions: set[int] = set()

    def call_names(st: Statement) -> list[str]:
        sites = extract_call_sites(st.text)
        names: list[str] = []

        def postorder(site: dict) -> None:
            for child in site["children"]:
                postorder(child)
            names.append(site["name"])

        for site in sites:
            if site.get("parent") is None:
                postorder(site)
        return names

    def walk_function(start: int) -> None:
        if start in walked_functions or start in active_functions or start not in visited:
            return
        frame = block_meta.get(start)
        if not frame or frame["kind"] != "function":
            return
        active_functions.add(start)
        walk_range(start + 1, frame["end"] + 1)
        active_functions.remove(start)
        walked_functions.add(start)

    def visit(i: int) -> None:
        if i in visited or stmts[i].kind == "comment":
            return
        visited.add(i)
        order.append(i)
        for name in call_names(stmts[i]):
            callee = function_starts.get(name)
            if callee is not None:
                walk_function(callee)

    def walk_range(begin: int, end: int) -> None:
        i = begin
        while i < end:
            st = stmts[i]
            visit(i)
            frame = block_meta.get(i)
            if st.kind == "function" and frame and frame["kind"] == "function":
                i = frame["end"] + 1
            else:
                i += 1

    walk_range(0, len(stmts))

    # Preserve anchors for uncalled functions and structurally unreachable code.
    for start in sorted(function_starts.values()):
        walk_function(start)
    for i, st in enumerate(stmts):
        if st.kind != "comment":
            visit(i)

    for number, i in enumerate(order, 1):
        stmts[i].sid = f"0x{number:06X}"


def annotate(stmts: list[Statement]) -> dict:
    for st in stmts:
        st.kind = classify(st.text)

    block_meta, end_to_start = match_blocks(stmts)
    assign_runtime_statement_ids(stmts, block_meta)

    # Function IDs use names so all references agree.
    funcs: dict[str, str] = {}
    for st in stmts:
        name = extract_function_name(st.text)
        if name:
            funcs[name] = stable_hex("function:" + name, "$", 6)

    # Calls get known function id or stable external symbol id.
    for st in stmts:
        for name in extract_calls(st.text):
            cid = funcs.get(name) or stable_hex("call:" + name, "$", 6)
            st.calls.append((name, cid))

    # Expression-level call sites preserve duplicates and nesting. Each site
    # gets its own A anchor even when multiple sites share the same $ symbol.
    call_sites_by_stmt: dict[int, list[dict]] = {}
    for i, st in enumerate(stmts):
        sites = extract_call_sites(st.text)
        for site in sites:
            site["cid"] = funcs.get(site["name"]) or stable_hex("call:" + site["name"], "$", 6)
            site["eid"] = stable_hex(
                f"eval:{st.sid}:{site['start']}:{site['name']}",
                "A",
                6,
            )
        call_sites_by_stmt[i] = sites

    # Helpers
    def next_exec(idx: int) -> int | None:
        j = idx + 1
        while j < len(stmts) and stmts[j].kind == "comment": j += 1
        return j if j < len(stmts) else None

    def target(idx: int | None) -> str:
        return f"@{stmts[idx].sid}" if idx is not None else "<exit>"

    # Find the innermost matched block that contains an index. Used for
    # break/return semantics and function-local flow annotations.
    def enclosing_blocks(idx: int) -> list[tuple[int, dict]]:
        found = []
        for start, fr in block_meta.items():
            if start < idx <= fr["end"]:
                found.append((start, fr))
        found.sort(key=lambda item: item[0], reverse=True)
        return found

    def function_scope(idx: int) -> int | None:
        fr = next(((start, meta) for start, meta in enclosing_blocks(idx) if meta["kind"] == "function"), None)
        if fr:
            return fr[0]
        if idx in block_meta and block_meta[idx]["kind"] == "function":
            return idx
        return None

    labels: dict[tuple[int | None, str], tuple[int, str]] = {}
    for i, st in enumerate(stmts):
        name = extract_label_name(st.text)
        if name:
            lid = stable_hex(f"label:{function_scope(i)}:{name}", "L", 6)
            labels[(function_scope(i), name)] = (i, lid)
            st.annotations.append(f"-- {lid} label {name}")

    # Function definition annotations.
    for i, st in enumerate(stmts):
        name = extract_function_name(st.text)
        if name:
            st.annotations.append(f"-- {funcs[name]} def {name}")

    # Flow annotation for matched blocks.
    for start, fr in block_meta.items():
        kind = fr["kind"]; end = fr["end"]
        after = next_exec(end)
        if kind == "if":
            arms = fr["arms"]
            # each if/elseif condition: true -> first body stmt, false -> next arm or after end
            for pos, arm_i in enumerate(arms):
                arm = stmts[arm_i]
                if arm.kind in ("if", "elseif"):
                    cond_id = stable_hex(f"cond:{arm.sid}", "C", 5)
                    arm.annotations.append(f"-- cond {cond_id}")
                    true_i = next_exec(arm_i)
                    false_i = arms[pos+1] if pos+1 < len(arms) else after
                    true_sid = f"@{stmts[true_i].sid}" if true_i is not None else "<exit>"
                    false_sid = f"@{stmts[false_i].sid}" if false_i is not None else "<exit>"
                    arm.annotations.append(f"-- branch true -> {true_sid} | false -> {false_sid}")
                    expr = condition_expr(arm.text, arm.kind)
                    parts, ops = split_short_circuit(expr) if expr else ([], [])
                    if ops:
                        step_ids = [
                            stable_hex(f"expr:{arm.sid}:{step}:{part}", "E", 5)
                            for step, part in enumerate(parts, 1)
                        ]
                        for step, part in enumerate(parts, 1):
                            eid = step_ids[step - 1]
                            route = resolve_expr_routes(short_circuit_route(ops, step, true_sid, false_sid), step_ids)
                            arm.annotations.append(f"-- cond-step {eid} [{step}] {part} | {route}")
                elif arm.kind == "else":
                    body_i = next_exec(arm_i)
                    if body_i is not None:
                        arm.annotations.append(f"-- branch -> @{stmts[body_i].sid}")
            # arm tails -> after end (best-effort: statement before next arm/end)
            boundaries = arms[1:] + [end]
            for arm_i, boundary in zip(arms, boundaries):
                tail = boundary - 1
                while tail > arm_i and stmts[tail].kind == "comment": tail -= 1
                if tail > arm_i and after is not None and stmts[tail].kind not in ("terminal",):
                    stmts[tail].annotations.append(f"-- next -> @{stmts[after].sid}")
        elif kind in ("while", "for"):
            hdr = stmts[start]
            cond_id = stable_hex(f"cond:{hdr.sid}", "C", 5)
            hdr.annotations.append(f"-- cond {cond_id}")
            body = next_exec(start)
            t = f"@{stmts[body].sid}" if body is not None else "<exit>"
            f = f"@{stmts[after].sid}" if after is not None else "<exit>"
            hdr.annotations.append(f"-- branch true -> {t} | false -> {f}")
            expr = condition_expr(hdr.text, hdr.kind)
            parts, ops = split_short_circuit(expr) if expr else ([], [])
            if ops:
                step_ids = [
                    stable_hex(f"expr:{hdr.sid}:{step}:{part}", "E", 5)
                    for step, part in enumerate(parts, 1)
                ]
                for step, part in enumerate(parts, 1):
                    eid = step_ids[step - 1]
                    route = resolve_expr_routes(short_circuit_route(ops, step, t, f), step_ids)
                    hdr.annotations.append(f"-- cond-step {eid} [{step}] {part} | {route}")
            tail = end - 1
            while tail > start and stmts[tail].kind == "comment": tail -= 1
            if tail > start and stmts[tail].kind != "terminal":
                stmts[tail].annotations.append(f"-- loop -> @{hdr.sid}")
        elif kind == "repeat":
            until = stmts[end]
            cond_id = stable_hex(f"cond:{until.sid}", "C", 5)
            until.annotations.append(f"-- cond {cond_id}")
            body = next_exec(start)
            f = f"@{stmts[body].sid}" if body is not None else "<exit>"
            t = f"@{stmts[after].sid}" if after is not None else "<exit>"
            until.annotations.append(f"-- branch true -> {t} | false -> {f}")
            expr = condition_expr(until.text, until.kind)
            parts, ops = split_short_circuit(expr) if expr else ([], [])
            if ops:
                step_ids = [
                    stable_hex(f"expr:{until.sid}:{step}:{part}", "E", 5)
                    for step, part in enumerate(parts, 1)
                ]
                for step, part in enumerate(parts, 1):
                    eid = step_ids[step - 1]
                    route = resolve_expr_routes(short_circuit_route(ops, step, t, f), step_ids)
                    until.annotations.append(f"-- cond-step {eid} [{step}] {part} | {route}")

        elif kind == "function":
            # Defining a function does not execute its body. At load/runtime the
            # closure is created and control continues after the matching end.
            # Calls still jump to this statement's $ anchor for navigation.
            hdr = stmts[start]
            hdr.annotations.append(f"-- next -> {target(after)}")

    # Terminal control flow. These annotations intentionally describe runtime
    # semantics instead of merely pointing at the next source line.
    for i, st in enumerate(stmts):
        if st.kind != "terminal":
            continue
        text = st.text.strip()
        if text == "break":
            loop = next(
                ((start, fr) for start, fr in enclosing_blocks(i) if fr["kind"] in ("while", "for", "repeat")),
                None,
            )
            after = next_exec(loop[1]["end"]) if loop else None
            st.annotations.append(f"-- break -> {target(after)}")
        elif text.startswith("return"):
            st.annotations.append("-- return -> <caller>")
        elif text.startswith("goto"):
            name = extract_goto_name(text)
            resolved = labels.get((function_scope(i), name)) if name else None
            if resolved:
                label_i, lid = resolved
                st.annotations.append(f"-- goto {lid} {name} -> @{stmts[label_i].sid}")
            else:
                st.annotations.append(f"-- goto {name or '?'} -> <label:unresolved>")

    # A function that reaches its closing `end` performs an implicit return.
    # The same `end` is skipped when the function is merely being defined;
    # that source-load edge is represented on the function header above.
    for end, start in end_to_start.items():
        if block_meta[start]["kind"] == "function":
            stmts[end].annotations.append("-- return -> <caller>")

    # Sequential path for ordinary statements. This makes the text itself a walkable CFG.
    for i, st in enumerate(stmts):
        if st.kind == "comment":
            continue
        has_flow = any(a.startswith(("-- branch", "-- next", "-- loop")) for a in st.annotations)
        if st.kind in ("stmt", "label") and not has_flow:
            nxt = next_exec(i)
            if nxt is not None:
                st.annotations.append(f"-- next -> @{stmts[nxt].sid}")

    def call_continuation(st: Statement, idx: int) -> str:
        """Return the control-flow destination after a call on this statement.

        A source-order next line is wrong at branch tails and loop tails, so
        prefer the CFG edge already attached to the statement. Calls embedded
        in a condition resume inside that same logical statement before the
        branch is chosen.
        """
        if st.kind in ("if", "elseif", "while", "for", "until"):
            return f"<resume:@{st.sid}>"
        if st.kind == "terminal":
            if st.text.strip().startswith("return"):
                return "<caller>"
            if st.text.strip() == "break":
                edge = next((a for a in st.annotations if a.startswith("-- break -> ")), None)
                return edge[len("-- break -> "):] if edge else "<exit>"
        for prefix in ("-- next -> ", "-- loop -> "):
            edge = next((a for a in st.annotations if a.startswith(prefix)), None)
            if edge:
                return edge[len(prefix):]
        return target(next_exec(idx))

    def callee_target(name: str) -> str:
        callee_stmt = next((x for x in stmts if extract_function_name(x.text) == name), None)
        return f"@{callee_stmt.sid}" if callee_stmt is not None else "<external>"

    # Nested calls are dependencies, but sibling argument evaluation order is
    # deliberately not invented. Lua does not provide a portable left-to-right
    # guarantee for function-call argument expressions.
    for i, st in enumerate(stmts):
        sites = call_sites_by_stmt[i]
        for site in sites:
            children = site["children"]
            if children:
                deps = ", ".join(f"{child['eid']}:{child['name']}" for child in children)
                sibling_order = "unspecified" if len(children) > 1 else "source"
                st.annotations.append(
                    f"-- eval {site['eid']} {site['name']} waits <- [{deps}] | sibling-order:{sibling_order}"
                )
            else:
                st.annotations.append(f"-- eval {site['eid']} {site['name']} ready")

            parent = site.get("parent")
            continuation = f"<eval:{parent['eid']}>" if parent is not None else call_continuation(st, i)
            st.annotations.append(
                f"-- {site['cid']} eval-call {site['eid']} {site['name']} -> {callee_target(site['name'])}"
                f" | return -> {continuation}"
            )

    for i, st in enumerate(stmts):
        for name, cid in st.calls:
            continuation = call_continuation(st, i)
            st.annotations.append(f"-- {cid} call {name} -> {callee_target(name)} | return -> {continuation}")

    return {
        "functions": funcs,
        "blocks": block_meta,
        "end_to_start": end_to_start,
        "labels": labels,
        "call_sites": call_sites_by_stmt,
    }


def render_lua(stmts: list[Statement]) -> str:
    lines = [
        "-- Generated by luare",
        "-- @0x...... statement; $0x...... callable; C0x... condition; E0x... condition-step; A0x... call-eval; L0x... label",
        "",
    ]
    indent = 0
    for st in stmts:
        if st.kind in ("end", "else", "elseif", "until"):
            indent = max(0, indent - 1)
        pref = "    " * indent
        if st.kind == "comment":
            lines.append(pref + st.text)
            continue
        lines.append(pref + f"-- @{st.sid} [src:{st.src_line}]")
        for ann in st.annotations:
            lines.append(pref + ann)
        lines.append(pref + st.text)
        if st.kind in ("if", "elseif", "else", "while", "for", "function", "do", "repeat"):
            indent += 1
    return "\n".join(lines) + "\n"


def anchorize_comment(text: str) -> str:
    esc = html.escape(text)
    esc = re.sub(r"@(?P<id>0x[0-9A-Fa-f]+)", lambda m: f'<a href="#stmt-{m.group("id")}" class="jump jump-statement">@{m.group("id")}</a>', esc)
    esc = re.sub(r"\$(?P<id>0x[0-9A-Fa-f]+)", lambda m: f'<a href="#sym-{m.group("id")}" class="jump jump-symbol">${m.group("id")}</a>', esc)
    esc = re.sub(
        r"(?<![\w$@])(?P<id>[ACEL]0x[0-9A-Fa-f]+)",
        lambda m: f'<a href="#nav-{m.group("id")}" class="jump jump-analysis">{m.group("id")}</a>',
        esc,
    )
    return esc


LUA_HIGHLIGHT_RE = re.compile(
    r'''(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
    r'''|(?P<number>\b(?:0[xX][0-9A-Fa-f]+|\d+(?:\.\d+)?)\b)'''
    r'''|(?P<keyword>\b(?:and|break|do|else|elseif|end|for|function|goto|if|in|local|not|or|repeat|return|then|until|while)\b)'''
    r'''|(?P<literal>\b(?:true|false|nil)\b)'''
    r'''|(?P<call>\b[A-Za-z_]\w*(?=\s*\())'''
    r'''|(?P<operator>\.\.|==|~=|<=|>=|[-+*/%^#=<>])'''
)


def highlight_lua(text: str) -> str:
    """Apply conservative, display-only Lua syntax highlighting."""
    chunks = []
    cursor = 0
    for match in LUA_HIGHLIGHT_RE.finditer(text):
        chunks.append(html.escape(text[cursor:match.start()]))
        token_type = match.lastgroup or "token"
        chunks.append(f'<span class="tok-{token_type}">{html.escape(match.group(0))}</span>')
        cursor = match.end()
    chunks.append(html.escape(text[cursor:]))
    return "".join(chunks)


def annotation_nav_id(text: str) -> str:
    """Return a navigation target id for annotation definitions."""
    patterns = (
        r"^-- cond (?P<id>C0x[0-9A-Fa-f]+)$",
        r"^-- cond-step (?P<id>E0x[0-9A-Fa-f]+)\b",
        r"^-- eval (?P<id>A0x[0-9A-Fa-f]+)\b",
        r"^-- (?P<id>L0x[0-9A-Fa-f]+) label\b",
    )
    for pattern in patterns:
        m = re.match(pattern, text)
        if m:
            return f' id="nav-{m.group("id")}"'
    return ""


def build_statement_cfg(stmts: list[Statement]) -> dict:
    """Build statement-level nodes and edges from resolved flow annotations."""
    nodes = []
    edges = []
    seen: set[tuple[str, str, str]] = set()
    block_meta, _ = match_blocks(stmts)
    function_ranges = []
    for start, frame in block_meta.items():
        if frame["kind"] == "function":
            name = extract_function_name(stmts[start].text) or f"<function@{stmts[start].src_line}>"
            function_ranges.append((start, frame["end"], name))

    def scope_for(index: int) -> str:
        # A declaration executes in its parent scope; only its deferred body
        # belongs to the declared function's graph.
        containing = [item for item in function_ranges if item[0] < index <= item[1]]
        return max(containing, key=lambda item: item[0])[2] if containing else "<main>"

    for index, st in enumerate(stmts):
        if st.kind == "comment":
            continue
        nodes.append({
            "id": st.sid,
            "text": st.text,
            "line": st.src_line,
            "kind": st.kind,
            "scope": scope_for(index),
        })

        def add(dst: str, label: str) -> None:
            key = (st.sid, dst, label)
            if key not in seen:
                seen.add(key)
                edges.append({"from": st.sid, "to": dst, "label": label})

        for ann in st.annotations:
            m = re.match(r"^-- branch true -> @(?P<t>0x[0-9A-F]+) \| false -> @(?P<f>0x[0-9A-F]+)$", ann)
            if m:
                add(m.group("t"), "true")
                add(m.group("f"), "false")
                continue
            m = re.match(r"^-- branch true -> @(?P<t>0x[0-9A-F]+) \| false -> <exit>$", ann)
            if m:
                add(m.group("t"), "true")
                continue
            m = re.match(r"^-- branch true -> <exit> \| false -> @(?P<f>0x[0-9A-F]+)$", ann)
            if m:
                add(m.group("f"), "false")
                continue
            m = re.match(r"^-- branch -> @(?P<dst>0x[0-9A-F]+)$", ann)
            if m:
                add(m.group("dst"), "branch")
                continue
            m = re.match(r"^-- (?P<label>next|loop|break) -> @(?P<dst>0x[0-9A-F]+)$", ann)
            if m:
                add(m.group("dst"), m.group("label"))
                continue
            m = re.match(r"^-- goto .* -> @(?P<dst>0x[0-9A-F]+)$", ann)
            if m:
                add(m.group("dst"), "goto")
                continue
            m = re.match(r"^-- \$0x[0-9A-F]+ call (?P<name>.*?) -> @(?P<dst>0x[0-9A-F]+) \|", ann)
            if m:
                add(m.group("dst"), f"call {m.group('name')}")

    return {"nodes": nodes, "edges": edges}


def build_cfg(stmts: list[Statement]) -> dict:
    """Coalesce straight-line statement runs into IDA-style basic blocks."""
    statement_cfg = build_statement_cfg(stmts)
    statement_nodes = statement_cfg["nodes"]
    statement_edges = statement_cfg["edges"]
    node_by_id = {node["id"]: node for node in statement_nodes}
    outgoing = {node["id"]: [] for node in statement_nodes}
    incoming = {node["id"]: [] for node in statement_nodes}
    for edge in statement_edges:
        if edge["from"] in outgoing and edge["to"] in incoming:
            outgoing[edge["from"]].append(edge)
            incoming[edge["to"]].append(edge)

    assigned: set[str] = set()
    blocks = []
    statement_to_block: dict[str, str] = {}
    ordered_nodes = sorted(statement_nodes, key=lambda node: int(node["id"][2:], 16))

    for first in ordered_nodes:
        if first["id"] in assigned:
            continue
        members = [first]
        assigned.add(first["id"])
        current = first
        while True:
            edges = outgoing[current["id"]]
            if len(edges) != 1 or edges[0]["label"] != "next":
                break
            next_id = edges[0]["to"]
            next_node = node_by_id.get(next_id)
            member_ids = {member["id"] for member in members}
            if (
                next_node is None
                or next_id in assigned
                or next_node["scope"] != first["scope"]
                or len(incoming[next_id]) != 1
                or any(edge["to"] in member_ids for edge in outgoing[next_id])
            ):
                break
            members.append(next_node)
            assigned.add(next_id)
            current = next_node

        block_id = first["id"]
        for member in members:
            statement_to_block[member["id"]] = block_id
        blocks.append({
            "id": block_id,
            "text": "\n".join(member["text"] for member in members),
            "line": first["line"],
            "end_line": members[-1]["line"],
            "kind": first["kind"] if len(members) == 1 else "block",
            "scope": first["scope"],
            "statements": [member["id"] for member in members],
        })

    block_edges = []
    seen: set[tuple[str, str, str]] = set()
    for edge in statement_edges:
        source = statement_to_block.get(edge["from"])
        target = statement_to_block.get(edge["to"])
        if source is None or target is None or source == target:
            continue
        key = (source, target, edge["label"])
        if key not in seen:
            seen.add(key)
            block_edges.append({"from": source, "to": target, "label": edge["label"]})

    return {"nodes": blocks, "edges": block_edges, "statement_to_block": statement_to_block}


LUA_KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function",
    "goto", "if", "in", "local", "nil", "not", "or", "repeat", "return", "then",
    "true", "until", "while",
}


def build_xrefs(stmts: list[Statement]) -> dict:
    """Build a conservative symbol/string cross-reference index."""
    function_names = {name for st in stmts if (name := extract_function_name(st.text))}
    symbols: dict[str, dict] = {}
    strings: dict[str, set[str]] = {}

    def symbol(name: str) -> dict:
        return symbols.setdefault(name, {
            "name": name,
            "kind": "function" if name in function_names else "variable",
            "definitions": set(),
            "writes": set(),
            "reads": set(),
            "calls": set(),
        })

    identifier_re = re.compile(r"(?<![\w])([A-Za-z_]\w*(?:(?:\.|:)[A-Za-z_]\w*)*)")
    string_re = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', re.X)

    for st in stmts:
        if st.kind == "comment":
            continue
        text = st.text.strip()
        definitions: set[str] = set()
        writes: set[str] = set()
        read_source = text
        function_name = extract_function_name(text)
        if function_name:
            read_source = ""
            definitions.add(function_name)
            match = re.search(r"\((.*?)\)", text)
            if match:
                definitions.update(
                    part.strip() for part in match.group(1).split(",")
                    if re.match(r"^[A-Za-z_]\w*$", part.strip())
                )
        else:
            local_match = re.match(r"^local\s+(.+?)(?:\s*=|$)", text)
            if local_match:
                definitions.update(
                    part.strip() for part in local_match.group(1).split(",")
                    if re.match(r"^[A-Za-z_]\w*$", part.strip())
                )
            for_match = re.match(r"^for\s+(.+?)\s+(?:in\b|=)", text)
            if for_match:
                definitions.update(
                    part.strip() for part in for_match.group(1).split(",")
                    if re.match(r"^[A-Za-z_]\w*$", part.strip())
                )
            assignment = find_top_level_assignment(text)
            if assignment is not None:
                lhs = re.sub(r"^local\s+", "", text[:assignment].strip())
                read_source = text[assignment + 1:]
                writes.update(
                    part.strip() for part in lhs.split(",")
                    if re.match(r"^[A-Za-z_]\w*(?:(?:\.|:)[A-Za-z_]\w*)*$", part.strip())
                )

        for name in definitions:
            entry = symbol(name)
            entry["definitions"].add(st.sid)
            entry["writes"].add(st.sid)
        for name in writes:
            symbol(name)["writes"].add(st.sid)
        for name, _ in st.calls:
            symbol(name)["calls"].add(st.sid)

        excluded = LUA_KEYWORDS | definitions
        for match in identifier_re.finditer(read_source):
            name = match.group(1)
            if name.split(".")[0].split(":")[0] not in excluded and name not in excluded:
                symbol(name)["reads"].add(st.sid)
        for match in string_re.finditer(text):
            strings.setdefault(match.group(0), set()).add(st.sid)

    serialized = []
    for entry in symbols.values():
        serialized.append({
            **{key: value for key, value in entry.items() if not isinstance(value, set)},
            "definitions": sorted(entry["definitions"]),
            "writes": sorted(entry["writes"]),
            "reads": sorted(entry["reads"]),
            "calls": sorted(entry["calls"]),
        })
    serialized.sort(key=lambda item: (item["kind"] != "function", item["name"].lower()))
    string_items = [
        {"value": value, "references": sorted(references)}
        for value, references in strings.items()
    ]
    string_items.sort(key=lambda item: item["value"])
    return {"symbols": serialized, "strings": string_items}


def build_project(inp: Path, stmts: list[Statement], existing: dict | None = None) -> dict:
    """Create or refresh a persistent luaRE analysis project."""
    existing = existing or {}
    user = existing.get("user") if isinstance(existing.get("user"), dict) else {}
    return {
        "version": 1,
        "source": str(inp.resolve()),
        "source_hash": hashlib.sha256(inp.read_bytes()).hexdigest(),
        "analysis": {
            "xrefs": build_xrefs(stmts),
            "cfg": build_cfg(stmts),
        },
        "user": {
            "renames": user.get("renames", {}),
            "symbol_comments": user.get("symbol_comments", {}),
            "statement_comments": user.get("statement_comments", {}),
            "bookmarks": user.get("bookmarks", []),
            "graph_layout": user.get("graph_layout", {}),
        },
    }


def render_html(
    stmts: list[Statement],
    meta: dict,
    title: str,
    trace_data: dict | None = None,
    live_debug: bool = False,
    project_data: dict | None = None,
    source_text: str | None = None,
) -> str:
    """Render source, runtime-order, and interactive CFG views."""
    definitions = {}
    for st in stmts:
        name = extract_function_name(st.text)
        if name:
            definitions[meta["functions"][name]] = st.sid

    def render_statement(st: Statement, statement_prefix: str, nav_prefix: str) -> str:
        name = extract_function_name(st.text)
        symbol_attr = f' data-symbol="{meta["functions"][name]}"' if name else ""
        rows = []

        def annotation_kind(annotation: str) -> str:
            if annotation.startswith("-- @"):
                return "anchor"
            if re.match(r"^-- \$0x.*\b(?:def|call|eval-call)\b", annotation):
                return "symbol"
            if annotation.startswith(("-- cond ", "-- cond-step ")):
                return "condition"
            if annotation.startswith("-- branch"):
                return "branch"
            if annotation.startswith("-- eval "):
                return "evaluation"
            if annotation.startswith(("-- next ", "-- loop ", "-- break ", "-- return ", "-- goto ")):
                return "flow"
            if re.match(r"^-- L0x.* label ", annotation):
                return "label"
            return "note"

        def annotation_row(annotation: str) -> str:
            anchor_attr = annotation_nav_id(annotation)
            if anchor_attr and nav_prefix != "nav-":
                anchor_attr = anchor_attr.replace('id="nav-', f'id="{nav_prefix}', 1)
            return (
                f'<div class="line annotation annotation-{annotation_kind(annotation)}"{anchor_attr}>'
                f'<span class="src"></span>'
                f'<code>{anchorize_comment(annotation)}</code></div>'
            )

        rows.append(annotation_row(f"-- @{st.sid} [src:{st.src_line}]"))
        rows.append(
            f'<div class="line code" id="{statement_prefix}{st.sid}" data-source-line="{st.src_line}"{symbol_attr}>'
            f'<span class="src">{st.src_line}</span><code>{highlight_lua(st.text)}</code></div>'
        )
        rows.extend(annotation_row(annotation) for annotation in st.annotations)
        return "".join(rows)

    source_rows = []
    for st in stmts:
        if st.kind == "comment":
            source_rows.append(
                f'<div class="line comment"><span class="src">{st.src_line}</span>'
                f'<code>{anchorize_comment(st.text)}</code></div>'
            )
        else:
            source_rows.append(
                f'<section class="statement-unit kind-{st.kind}" data-statement="{st.sid}">'
                f'{render_statement(st, "stmt-", "nav-")}</section>'
            )

    runtime_rows = []
    runtime_order = sorted(
        (st for st in stmts if st.kind != "comment"),
        key=lambda st: int(st.sid[2:], 16),
    )
    for st in runtime_order:
        runtime_rows.append(
            f'<section class="statement-unit flow-unit kind-{st.kind}" data-statement="{st.sid}">'
            f'{render_statement(st, "flow-stmt-", "flow-nav-")}</section>'
        )

    def script_json(value) -> str:
        return (
            json.dumps(value, ensure_ascii=True)
            .replace("<", "\\u003C")
            .replace(">", "\\u003E")
            .replace("&", "\\u0026")
        )

    symbol_map = script_json({key[1:]: sid for key, sid in definitions.items()})
    cfg_json = script_json(build_cfg(stmts))
    xref_json = script_json(build_xrefs(stmts))
    trace_json = script_json(trace_data)
    statement_text_json = script_json({st.sid: st.text for st in stmts if st.kind != "comment"})
    statement_info_json = script_json([
        {"sid": st.sid, "line": st.src_line, "text": st.text, "html": highlight_lua(st.text)}
        for st in stmts if st.kind != "comment"
    ])
    if source_text is None:
        source_lines = [""] * max((st.src_line for st in stmts), default=0)
        for st in stmts:
            separator = "; " if source_lines[st.src_line - 1] else ""
            source_lines[st.src_line - 1] += separator + st.text
        source_text = "\n".join(source_lines)
    source_text_json = script_json(source_text)
    source_name_json = script_json(title)
    source_digest_json = script_json(hashlib.sha256(source_text.encode("utf-8")).hexdigest())
    live_debug_json = "true" if live_debug else "false"
    project_json = script_json(project_data)
    page = '''<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
:root { color-scheme:dark; --bar-h:58px; --bg:#0b1016; --surface:#101720; --surface-2:#151e29; --surface-3:#1b2633; --line:#273444; --line-soft:#182330; --muted:#708095; --text:#d8e2ef; --accent:#78b9d4; --blue:#82aaff; --purple:#c099ff; --green:#9ecea8; --amber:#e5b875; --red:#e1848f; }
* { box-sizing:border-box; scrollbar-color:#334357 #0b1016; scrollbar-width:thin; }
::selection { background:#31516f; color:#f3f7fc; }
body { margin:0; background:var(--bg); color:var(--text); font:13.5px/1.55 "Cascadia Code","JetBrains Mono","SFMono-Regular",Consolas,monospace; letter-spacing:-.01em; }
body.graph-mode,body.debug-mode,body.analysis-mode,body.patch-mode { overflow:hidden; }
#bar { position:sticky; top:0; z-index:20; min-height:var(--bar-h); display:flex; align-items:center; gap:20px; padding:9px 18px; background:#101720ed; border-bottom:1px solid #2b3949; box-shadow:0 8px 28px #05080d80; backdrop-filter:blur(16px); }
#brand { display:inline-flex; align-items:center; height:34px; padding:0 11px; border:1px solid #345064; border-radius:8px; background:linear-gradient(145deg,#172633,#111b24); color:#9bd4e8; font-weight:800; letter-spacing:.04em; box-shadow:inset 0 1px #ffffff0a; }
#tabs { display:flex; align-self:stretch; gap:3px; padding:3px; border:1px solid #253241; border-radius:9px; background:#0b1119; }
.tab { position:relative; border:0; border-radius:6px; padding:0 14px; background:transparent; color:#7f8fa4; font:inherit; font-size:12.5px; font-weight:600; line-height:1; cursor:pointer; transition:background .15s,color .15s,box-shadow .15s; }
.tab:hover { background:#17212d; color:#c9d5e3; }
.tab[aria-selected="true"] { background:#202d3b; color:#e6f0fa; box-shadow:0 1px 5px #0008,inset 0 1px #ffffff0a; }
.tab[aria-selected="true"]::after { content:""; position:absolute; left:14px; right:14px; bottom:2px; height:2px; border-radius:2px; background:var(--accent); box-shadow:0 0 10px #78b9d477; }
#hint { margin-left:auto; color:#66778c; white-space:nowrap; font-size:11.5px; }
.view { display:none; }
.view.active { display:block; }
.code-view { width:100%; max-width:1800px; margin:0 auto; padding:14px 12px 70px; }
.statement-unit { margin:0 0 7px; border:1px solid var(--line-soft); border-left:3px solid #2b3b4c; border-radius:7px; background:#0e151e; box-shadow:0 1px 0 #ffffff04; overflow:hidden; transition:border-color .14s,background .14s,box-shadow .14s; }
.statement-unit:hover { border-color:#33465b; border-left-color:#54728e; background:#101923; box-shadow:0 4px 14px #05080d55; }
.statement-unit.kind-if,.statement-unit.kind-elseif,.statement-unit.kind-while,.statement-unit.kind-for,.statement-unit.kind-repeat,.statement-unit.kind-until { border-left-color:#987747; }
.statement-unit.kind-function { border-left-color:#7662a5; }
.statement-unit.kind-terminal { border-left-color:#995764; }
.line { display:flex; min-height:19px; line-height:19px; white-space:pre-wrap; scroll-margin-top:78px; }
.src { width:64px; flex:none; text-align:right; padding-right:13px; color:#4f6075; user-select:none; font-size:11.5px; }
code { flex:1; min-width:0; padding-right:20px; overflow-wrap:anywhere; }
.code { min-height:31px; align-items:center; background:linear-gradient(90deg,#151e29 0,#111923 72%,#101720 100%); border-top:1px solid #1d2936; border-bottom:1px solid #1d2936; }
.code .src { color:#71839a; font-variant-numeric:tabular-nums; }
.code code { color:var(--text); font-size:14px; font-weight:500; line-height:22px; padding-top:4px; padding-bottom:4px; }
.annotation { color:var(--muted); font-size:11.5px; }
.annotation code { padding-top:1px; padding-bottom:1px; }
.annotation-anchor { min-height:22px; align-items:center; background:#0c131b; color:#65778d; }
.annotation-anchor code { font-size:11px; letter-spacing:.015em; }
.annotation:not(.annotation-anchor) code::before { content:"↳"; display:inline-block; width:19px; color:#3f5369; }
.annotation-flow code { color:#7899ad; }
.annotation-branch code,.annotation-condition code { color:#a89470; }
.annotation-symbol code { color:#9b8abb; }
.annotation-evaluation code { color:#748da3; }
.annotation-label code { color:#9b8f72; }
.comment { min-height:27px; align-items:center; margin:5px 3px; border-left:2px solid #315944; border-radius:0 5px 5px 0; background:#0d1817; }
.comment code { color:#6f9b82; font-style:italic; padding-top:3px; padding-bottom:3px; }
.flow-unit { margin-bottom:9px; }
a.jump { text-decoration:none; border-bottom:1px dotted currentColor; cursor:pointer; font-weight:650; }
a.jump-statement { color:#71b7d3; }
a.jump-symbol { color:#b49ae5; }
a.jump-analysis { color:#c49b69; }
a.jump:hover { color:#f1ca84; text-shadow:0 0 12px #e7ae5266; }
.tok-keyword { color:#c792ea; font-weight:650; }
.tok-string { color:#a8cf8c; }
.tok-number { color:#f0b878; }
.tok-literal { color:#ef8f9c; }
.tok-call { color:#79c7d9; }
.tok-operator { color:#89a8c4; }
.flash { animation:flash .8s ease-out; }
.statement-unit.runtime-current { border-color:#5699b8; border-left-color:#83c7df; box-shadow:0 0 0 1px #5699b844,0 0 24px #2c789044; }
@keyframes flash { from { background:#34536a; box-shadow:inset 3px 0 #8fd0e8; } to { background:transparent; box-shadow:inset 3px 0 transparent; } }
#graph-view { position:relative; height:calc(100vh - var(--bar-h)); overflow:hidden; background-color:#090e14; background-image:radial-gradient(#253444 1px,transparent 1px),linear-gradient(#0d141d99,#090e14); background-size:24px 24px,100% 100%; touch-action:none; }
#graph-world { position:absolute; left:0; top:0; transform-origin:0 0; }
#graph-edges { position:absolute; left:0; top:0; width:1px; height:1px; overflow:visible; pointer-events:none; }
#graph-nodes { position:absolute; left:0; top:0; }
.graph-node { position:absolute; width:280px; min-height:72px; border:1px solid #3b4c60; border-radius:9px; background:#111923; box-shadow:0 10px 28px #020408aa, inset 0 1px #ffffff08; cursor:grab; user-select:none; overflow:hidden; }
.graph-node:active { cursor:grabbing; }
.graph-node.control { border-color:#8d704b; }
.graph-node.terminal { border-color:#925664; }
.node-head { display:flex; justify-content:space-between; gap:8px; padding:7px 10px; background:linear-gradient(#1d2936,#19232f); border-bottom:1px solid #304052; color:#8dccdf; font-size:11.5px; font-weight:700; }
.node-line { color:#60738a; font-weight:500; }
.node-code { padding:11px; color:#dbe6f2; line-height:1.5; white-space:pre-wrap; overflow-wrap:anywhere; }
.graph-edge { fill:none; stroke:#607186; stroke-width:1.6; marker-end:url(#arrow); }
.graph-edge.true { stroke:#79b58a; }
.graph-edge.false { stroke:#cc7a7a; }
.graph-edge.loop, .graph-edge.goto { stroke:#c49a62; stroke-dasharray:5 4; }
.graph-edge.call { stroke:#a58bd4; stroke-dasharray:3 3; }
.edge-label { fill:#8495aa; font-size:11px; font-weight:600; paint-order:stroke; stroke:#090e14; stroke-width:5px; stroke-linejoin:round; }
#graph-tools { position:absolute; z-index:5; top:14px; left:14px; display:flex; gap:6px; align-items:center; padding:8px; border:1px solid #304155; border-radius:10px; background:#101821e8; box-shadow:0 10px 30px #020408aa,inset 0 1px #ffffff08; backdrop-filter:blur(12px); }
#graph-tools button,#scope-select { height:31px; border:1px solid #35465a; border-radius:6px; background:#192431; color:#cbd8e5; font:inherit; font-size:12px; font-weight:600; line-height:1; cursor:pointer; }
#graph-tools button { min-width:33px; padding:0 9px; }
#graph-tools button:hover,#scope-select:hover { border-color:#52708b; background:#202e3d; }
#graph-tools button:disabled { opacity:.35; cursor:default; border-color:#2d3948; background:#141d27; }
#scope-select { max-width:250px; padding:0 9px; }
#graph-page { min-width:48px; color:#9bacc0; text-align:center; font-size:11.5px; font-variant-numeric:tabular-nums; }
#graph-status { margin-left:6px; padding-right:4px; color:#718399; font-size:11.5px; white-space:nowrap; }
#debug-view { height:calc(100vh - var(--bar-h)); background:#0a0f15; }
#analysis-view { height:calc(100vh - var(--bar-h)); background:#0a0f15; }
#patch-view { height:calc(100vh - var(--bar-h)); background:#0a0f15; }
#patch-shell { height:100%; display:grid; grid-template-rows:auto minmax(0,1fr); }
#patch-tools { min-height:48px; display:flex; align-items:center; gap:7px; padding:8px 14px; border-bottom:1px solid #263444; background:#101720; }
#patch-tools button { height:31px; padding:0 11px; border:1px solid #35485c; border-radius:6px; background:#192532; color:#d2deea; font:inherit; font-size:12px; font-weight:600; cursor:pointer; }
#patch-tools button:hover { border-color:#587993; background:#21313f; }
#patch-status { margin-left:auto; color:#8395a9; font-size:11.5px; }
#patch-note { color:#c39b68; font-size:11.5px; }
#patch-editors { min-height:0; display:grid; grid-template-columns:1fr 1fr; }
.patch-pane { min-width:0; min-height:0; display:grid; grid-template-rows:34px minmax(0,1fr); }
.patch-pane:first-child { border-right:1px solid #2a394a; }
.patch-heading { display:flex; align-items:center; padding:0 13px; border-bottom:1px solid #22303f; background:#111923; color:#8396aa; font-size:11px; font-weight:750; letter-spacing:.06em; text-transform:uppercase; }
.patch-editor { width:100%; height:100%; min-width:0; min-height:0; resize:none; border:0; outline:0; margin:0; padding:14px 16px 60px; background:#0b1118; color:#d5dfeb; caret-color:#8fd0e5; font:13.5px/1.55 "Cascadia Code","JetBrains Mono","SFMono-Regular",Consolas,monospace; tab-size:2; white-space:pre; overflow:auto; }
.patch-editor[readonly] { background:#0d131a; color:#728397; }
#analysis-shell { height:100%; display:grid; grid-template-columns:330px minmax(0,1fr); }
#analysis-sidebar { min-height:0; display:grid; grid-template-rows:auto auto minmax(0,1fr) auto; border-right:1px solid #263444; background:#0d141c; }
#analysis-search { height:35px; margin:11px; border:1px solid #314256; border-radius:7px; background:#101923; color:#d4e0ec; padding:0 10px; font:inherit; outline:none; }
#analysis-search:focus { border-color:#6091aa; box-shadow:0 0 0 2px #3e718b33; }
#analysis-types { display:flex; gap:5px; padding:0 11px 10px; }
.analysis-type { border:1px solid #304154; border-radius:6px; background:#151f2a; color:#8091a5; padding:6px 10px; font:inherit; font-size:11.5px; font-weight:600; line-height:1; cursor:pointer; }
.analysis-type.active { border-color:#507890; background:#1b2b38; color:#bcd1df; }
#analysis-list { min-height:0; overflow:auto; }
.analysis-item { padding:8px 11px; border-top:1px solid #17232f; cursor:pointer; }
.analysis-item:hover,.analysis-item.selected { background:#162331; }
.analysis-name { color:#c5d3e1; overflow-wrap:anywhere; }
.analysis-item.selected .analysis-name { color:#8fd0e5; }
.analysis-meta { margin-top:2px; color:#607287; font-size:10.5px; }
#analysis-detail { min-height:0; overflow:auto; padding:18px; }
.analysis-heading { margin:0 0 4px; color:#dce7f2; font-size:19px; font-weight:700; }
.analysis-kind { color:#74869b; font-size:11.5px; }
.xref-group { margin-top:18px; border:1px solid #253444; border-radius:8px; background:#101923; overflow:hidden; }
.xref-title { padding:7px 10px; border-bottom:1px solid #263646; background:#17222e; color:#91a4b8; font-size:11px; font-weight:750; text-transform:uppercase; }
.xref-list { display:flex; flex-wrap:wrap; gap:7px; padding:10px; }
.xref-link { border:1px solid #355064; border-radius:5px; background:#12212b; color:#80bfd2; padding:5px 8px; font:inherit; font-size:11.5px; cursor:pointer; }
.xref-link:hover { border-color:#65a1bc; color:#b8e2ef; }
.xref-link.bookmarked { border-color:#a8874f; color:#e0b974; box-shadow:inset 0 0 0 1px #a8874f33; }
#project-tools { display:flex; gap:6px; padding:9px; border-top:1px solid #263444; }
#project-tools button,.project-editor button { border:1px solid #35485c; border-radius:6px; background:#182431; color:#aebfd0; padding:6px 9px; font:inherit; font-size:11px; cursor:pointer; }
#project-import { display:none; }
.project-editor { display:grid; grid-template-columns:100px minmax(0,1fr); gap:8px 10px; margin-top:15px; padding:12px; border:1px solid #2a3a4b; border-radius:8px; background:#101923; }
.project-editor label { color:#718399; padding-top:6px; }
.project-editor input,.project-editor textarea { width:100%; border:1px solid #304357; border-radius:6px; background:#0c141d; color:#cedae6; padding:7px 9px; font:inherit; resize:vertical; }
.project-editor button { grid-column:2; justify-self:start; }
#debug-shell { height:100%; display:grid; grid-template-rows:auto minmax(0,1fr); }
#debug-tools { display:flex; align-items:center; gap:7px; min-height:50px; padding:8px 14px; border-bottom:1px solid #263444; background:#101720; }
#debug-tools button { height:32px; min-width:34px; padding:0 10px; border:1px solid #35485c; border-radius:6px; background:#192532; color:#d2deea; font:inherit; font-size:12px; font-weight:600; line-height:1; cursor:pointer; }
#debug-tools button:hover { border-color:#587993; background:#21313f; }
#debug-tools button:disabled { opacity:.35; cursor:default; }
#debug-tools input { width:82px; height:32px; border:1px solid #35485c; border-radius:6px; background:#0d151e; color:#d2deea; padding:0 8px; font:inherit; }
#debug-tools #live-input { width:180px; }
.live-separator { width:1px; height:24px; margin:0 3px; background:#304052; }
#live-status { min-width:64px; color:#d2a66e; font-size:11.5px; }
#trace-position { min-width:92px; color:#a7b7c9; text-align:center; font-variant-numeric:tabular-nums; }
#trace-summary { margin-left:auto; color:#718398; font-size:11.5px; }
#debug-content { min-height:0; display:grid; grid-template-columns:minmax(500px,64%) minmax(340px,36%); grid-template-rows:minmax(260px,1fr) minmax(120px,24%); }
#debug-source { min-height:0; overflow:auto; grid-column:1; grid-row:1; padding:8px 0 20px; border-right:1px solid #263444; border-bottom:1px solid #263444; background:#0b1118; }
.debug-source-row { position:relative; display:grid; grid-template-columns:26px 58px minmax(0,1fr); min-height:28px; align-items:stretch; border-left:3px solid transparent; color:#d4deea; cursor:default; }
.debug-source-row:hover { background:#111d28; }
.debug-source-row.cursor { background:#162331; }
.debug-source-row.current { border-left-color:#e3b46f; background:linear-gradient(90deg,#3b3020 0,#211e19 18%,#141b22 100%); box-shadow:inset 0 1px #d8aa5d22,inset 0 -1px #d8aa5d22; }
.debug-source-row.current .debug-code::before { content:"▶"; position:absolute; left:-18px; color:#efbd70; }
.debug-breakpoint { position:relative; border:0; background:transparent; cursor:pointer; }
.debug-breakpoint::after { content:""; position:absolute; width:10px; height:10px; left:7px; top:9px; border:1px solid #526274; border-radius:50%; opacity:.38; }
.debug-source-row:hover .debug-breakpoint::after { opacity:.8; }
.debug-source-row.has-breakpoint .debug-breakpoint::after { border-color:#f18691; background:#d95765; box-shadow:0 0 8px #d9576577; opacity:1; }
.debug-line { padding:4px 12px 3px 0; color:#5c6e83; text-align:right; font-size:11.5px; font-variant-numeric:tabular-nums; user-select:none; }
.debug-code { position:relative; min-width:0; padding:3px 18px 3px 5px; white-space:pre-wrap; overflow-wrap:anywhere; font-size:13.5px; line-height:22px; }
#trace-events { min-height:0; overflow:auto; grid-column:1; grid-row:2; border-right:1px solid #263444; background:#0c1219; }
.trace-event { display:grid; grid-template-columns:58px 76px 72px minmax(0,1fr); gap:7px; align-items:center; min-height:34px; padding:5px 10px; border-bottom:1px solid #17222e; color:#8292a6; cursor:pointer; }
.trace-event:hover { background:#121d28; }
.trace-event.selected { background:#172838; color:#d5e1ed; box-shadow:inset 3px 0 #72b7d2; }
.trace-seq { color:#506176; text-align:right; font-size:11px; }
.trace-kind { color:#b59add; font-weight:700; font-size:11px; }
.trace-location { color:#d0a56e; font-size:11px; }
.trace-code { overflow:hidden; color:#b8c6d6; text-overflow:ellipsis; white-space:nowrap; }
#debug-inspector { min-height:0; overflow:auto; grid-column:2; grid-row:1 / 3; padding:12px; background:#0d141c; }
.debug-empty { display:grid; place-items:center; height:100%; padding:40px; color:#718296; text-align:center; line-height:1.8; }
.inspect-grid { display:grid; grid-template-columns:repeat(2,minmax(260px,1fr)); gap:10px; }
.inspect-panel { min-width:0; border:1px solid #253343; border-radius:8px; background:#101923; overflow:hidden; }
.inspect-panel.wide { grid-column:1/-1; }
.inspect-title { padding:7px 10px; border-bottom:1px solid #263646; background:#17222e; color:#9fb1c4; font-size:11px; font-weight:750; letter-spacing:.055em; text-transform:uppercase; }
.inspect-body { max-height:280px; overflow:auto; }
.value-row,.stack-row,.console-row { display:grid; grid-template-columns:minmax(90px,32%) minmax(0,1fr); gap:10px; padding:6px 9px; border-bottom:1px solid #192531; }
.value-row.editable { grid-template-columns:minmax(90px,32%) minmax(0,1fr) 42px; align-items:center; }
.value-edit { width:40px; height:24px; border:1px solid #34485b; border-radius:5px; background:#172431; color:#82bdd0; cursor:pointer; font:10.5px/1 inherit; }
.value-edit:hover { border-color:#6391aa; background:#203242; color:#c2e3ee; }
.variables-state { grid-column:1/-1; display:flex; align-items:center; min-height:34px; padding:7px 10px; border:1px solid #294154; border-radius:7px; background:#10202b; color:#8fc3d5; font-size:11.5px; }
.variables-state.readonly { border-color:#343f4d; background:#121920; color:#77889b; }
.value-name { color:#80bfd1; overflow-wrap:anywhere; }
.value-data { color:#c5d2df; white-space:pre-wrap; overflow-wrap:anywhere; }
.value-type { color:#617389; }
.stack-row { grid-template-columns:38px minmax(0,1fr) 70px; color:#aebdcc; }
.stack-line { color:#697c92; text-align:right; }
.console-row { display:block; color:#a9c6ae; white-space:pre-wrap; }
.event-meta { display:grid; grid-template-columns:110px minmax(0,1fr); gap:6px 12px; padding:9px; }
.event-meta dt { color:#66798f; }
.event-meta dd { margin:0; color:#c4d1de; overflow-wrap:anywhere; }
@media (max-width:800px) { #hint { display:none; } #bar { gap:10px; padding-left:10px; padding-right:10px; } .tab { padding:0 9px; } .code-view { padding-left:5px; padding-right:5px; } }
@media (max-width:980px) { #debug-content { grid-template-columns:1fr; grid-template-rows:minmax(260px,48%) 150px minmax(220px,1fr); } #debug-source { grid-column:1; grid-row:1; border-right:0; } #trace-events { grid-column:1; grid-row:2; border-right:0; border-bottom:1px solid #263444; } #debug-inspector { grid-column:1; grid-row:3; } .inspect-grid { grid-template-columns:1fr; } }
@media (max-width:760px) { #analysis-shell { grid-template-columns:240px minmax(0,1fr); } }
@media (max-width:800px) { #patch-editors { grid-template-columns:1fr; grid-template-rows:38% 62%; } .patch-pane:first-child { border-right:0; border-bottom:1px solid #2a394a; } }
</style></head>
<body>
<header id="bar"><span id="brand">luaRE</span><nav id="tabs" aria-label="Views">
<button class="tab" data-view="source" aria-selected="true">Source</button>
<button class="tab" data-view="flow" aria-selected="false">Execution Flow</button>
<button class="tab" data-view="graph" aria-selected="false">Graph</button>
<button class="tab" data-view="analysis" aria-selected="false">Analysis</button>
<button class="tab" data-view="patch" aria-selected="false">Patch</button>
<button class="tab" data-view="debug" aria-selected="false">Debug Trace</button>
</nav><span id="hint">click anchors to jump · Backspace / Alt+Left to return</span></header>
<main>
<div id="source-view" class="view code-view active">__SOURCE_ROWS__</div>
<div id="flow-view" class="view code-view">__FLOW_ROWS__</div>
<div id="graph-view" class="view">
  <div id="graph-tools"><select id="scope-select" title="Graph scope"></select>
    <button id="graph-prev" title="Previous node range">‹</button><span id="graph-page">1 / 1</span><button id="graph-next" title="Next node range">›</button>
    <button id="zoom-out" title="Zoom out">−</button><button id="zoom-in" title="Zoom in">+</button>
    <button id="fit-graph" title="Fit graph">Fit</button><button id="reset-graph" title="Reset node layout">Reset</button>
    <span id="graph-status"></span></div>
  <div id="graph-world"><svg id="graph-edges"><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10z" fill="#607186"/></marker></defs></svg><div id="graph-nodes"></div></div>
</div>
<div id="analysis-view" class="view"><div id="analysis-shell">
  <aside id="analysis-sidebar"><input id="analysis-search" type="search" placeholder="Search symbols / strings">
    <div id="analysis-types"><button class="analysis-type active" data-analysis-type="symbols">Symbols</button><button class="analysis-type" data-analysis-type="strings">Strings</button></div>
    <div id="analysis-list"></div><div id="project-tools"><button id="project-export">Export Project</button><button id="project-import-button">Import</button><input id="project-import" type="file" accept="application/json"></div></aside>
    <section id="analysis-detail"><div class="debug-empty">Select a symbol or string to inspect cross-references.</div></section>
</div></div>
<div id="patch-view" class="view"><div id="patch-shell">
  <div id="patch-tools"><button id="patch-save">Save .patched.lua</button><button id="patch-reset">Reset Draft</button><button id="patch-copy">Copy</button><span id="patch-note">Browser-only draft · current debug process is unchanged</span><span id="patch-status">Unmodified</span></div>
  <div id="patch-editors"><section class="patch-pane"><div class="patch-heading">Original</div><textarea id="patch-original" class="patch-editor" readonly spellcheck="false"></textarea></section><section class="patch-pane"><div class="patch-heading">Patched Draft</div><textarea id="patch-draft" class="patch-editor" spellcheck="false"></textarea></section></div>
</div></div>
<div id="debug-view" class="view"><div id="debug-shell">
  <div id="debug-tools"><button id="trace-first" title="First event">|‹</button><button id="trace-prev" title="Previous event">‹</button>
    <span id="trace-position">0 / 0</span><button id="trace-next" title="Next event">›</button><button id="trace-last" title="Last event">›|</button>
    <button id="trace-source" title="Show selected event in Source">Source</button>
    <span class="live-separator live-only" hidden></span><button class="live-only" id="live-restart" title="Restart target (F2)" hidden>Restart F2</button><button class="live-only" id="live-step" title="Step Into (F7)" hidden>Step F7</button><button class="live-only" id="live-over" title="Step Over (F8)" hidden>Over F8</button>
    <button class="live-only" id="live-out" title="Step Out (Shift+F11)" hidden>Out</button><button class="live-only" id="live-runto" title="Run to selected source line (F4)" hidden>Run to Cursor F4</button><button class="live-only" id="live-continue" title="Continue (F5)" hidden>Continue F5</button>
    <input class="live-only" id="live-break-line" hidden type="number" min="1" placeholder="line"><button class="live-only" id="live-break" hidden>Break</button><span class="live-only" id="live-status" hidden></span>
    <input class="live-only" id="live-input" hidden type="text" placeholder="program input"><button class="live-only" id="live-input-send" hidden>Send Input</button>
    <span id="trace-summary"></span></div>
  <div id="debug-content"><div id="debug-source"></div><div id="trace-events"></div><div id="debug-inspector"></div></div>
</div></div>
</main>
<script>
const symbolMap = __SYMBOL_MAP__;
const graphData = __CFG_JSON__;
const xrefData = __XREF_JSON__;
const traceData = __TRACE_JSON__;
const statementText = __STATEMENT_TEXT_JSON__;
const originalStatementText = {...statementText};
const statementInfo = __STATEMENT_INFO_JSON__;
const originalSource = __SOURCE_TEXT_JSON__;
const sourceName = __SOURCE_NAME_JSON__;
const sourceDigest = __SOURCE_DIGEST_JSON__;
const liveDebug = __LIVE_DEBUG__;
const embeddedProject = __PROJECT_JSON__;
const historyStack = [];
let activeView = 'source';
const scrollByView = {source:0, flow:0};

function switchView(name) {
  if (name === activeView) return;
  if (activeView === 'source' || activeView === 'flow') scrollByView[activeView] = window.scrollY;
  activeView = name;
  document.querySelectorAll('.tab').forEach(button => button.setAttribute('aria-selected', String(button.dataset.view === name)));
  document.querySelectorAll('.view').forEach(view => view.classList.toggle('active', view.id === name + '-view'));
  document.body.classList.toggle('graph-mode', name === 'graph');
  document.body.classList.toggle('debug-mode', name === 'debug');
  document.body.classList.toggle('analysis-mode', name === 'analysis');
  document.body.classList.toggle('patch-mode', name === 'patch');
  if (name === 'graph') initGraph();
  else if (name === 'debug') initTrace();
  else if (name === 'analysis') initAnalysis();
  else if (name === 'patch') initPatch();
  else requestAnimationFrame(() => window.scrollTo({top:scrollByView[name] || 0}));
}
document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));

function statementTarget(sid) {
  const prefix = activeView === 'flow' ? 'flow-stmt-' : 'stmt-';
  return document.getElementById(prefix + sid) || document.getElementById('stmt-' + sid);
}
function targetFor(href) {
  if (href.startsWith('#sym-')) { const sid=symbolMap[href.slice(5)]; return sid ? statementTarget(sid) : null; }
  if (href.startsWith('#stmt-')) return statementTarget(href.slice(6));
  if (href.startsWith('#nav-') && activeView === 'flow') return document.getElementById('flow-' + href.slice(1)) || document.querySelector(href);
  return document.querySelector(href);
}
document.addEventListener('click', event => {
  const anchor=event.target.closest('a.jump'); if (!anchor) return; event.preventDefault();
  const target=targetFor(anchor.getAttribute('href')); if (!target) return;
  historyStack.push({view:activeView,top:window.scrollY});
  target.scrollIntoView({behavior:'smooth',block:'center'}); target.classList.add('flash'); setTimeout(() => target.classList.remove('flash'),900);
});
function goBack() {
  const previous=historyStack.pop(); if (!previous) return; switchView(previous.view);
  requestAnimationFrame(() => window.scrollTo({top:previous.top,behavior:'smooth'}));
}
document.addEventListener('keydown', event => {
  if ((event.key==='Backspace' && !/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) || (event.altKey && event.key==='ArrowLeft')) { event.preventDefault(); goBack(); }
});

const project = embeddedProject || {version:1,source:document.title,source_hash:document.title,analysis:{xrefs:xrefData,cfg:graphData},user:{renames:{},symbol_comments:{},statement_comments:{},bookmarks:[],graph_layout:{}}};
project.user ||= {}; project.user.renames ||= {}; project.user.symbol_comments ||= {}; project.user.statement_comments ||= {}; project.user.bookmarks ||= []; project.user.graph_layout ||= {};
const projectStorageKey='luare-project:'+project.source_hash;
try { const stored=JSON.parse(localStorage.getItem(projectStorageKey)); if (stored?.user) project.user={...project.user,...stored.user}; } catch (_) {}
function saveProjectLocal() { try { localStorage.setItem(projectStorageKey,JSON.stringify(project)); } catch (_) {} }
function renamed(name) { return project.user.renames[name] || name; }
const analysis = {initialized:false,type:'symbols',selected:null};
const analysisList=document.getElementById('analysis-list'), analysisDetail=document.getElementById('analysis-detail'), analysisSearch=document.getElementById('analysis-search');
function initAnalysis() {
  if (analysis.initialized) return; analysis.initialized=true;
  analysisSearch.addEventListener('input',renderAnalysisList);
  document.querySelectorAll('.analysis-type').forEach(button => button.addEventListener('click',() => {
    analysis.type=button.dataset.analysisType; analysis.selected=null;
    document.querySelectorAll('.analysis-type').forEach(item => item.classList.toggle('active',item===button)); renderAnalysisList();
    analysisDetail.innerHTML='<div class="debug-empty">Select an item to inspect cross-references.</div>';
  }));
  document.getElementById('project-export').addEventListener('click',exportProject);
  document.getElementById('project-import-button').addEventListener('click',() => document.getElementById('project-import').click());
  document.getElementById('project-import').addEventListener('change',importProject);
  renderAnalysisList();
}
function analysisItems() { return analysis.type==='symbols' ? xrefData.symbols : xrefData.strings; }
function renderAnalysisList() {
  analysisList.textContent=''; const query=analysisSearch.value.trim().toLowerCase();
  analysisItems().filter(item => ((item.name ? renamed(item.name) : item.value)+' '+(item.name || '')).toLowerCase().includes(query)).slice(0,2000).forEach(item => {
    const row=document.createElement('div'); row.className='analysis-item'+(analysis.selected===item?' selected':'');
    const name=document.createElement('div'); name.className='analysis-name'; name.textContent=item.name ? renamed(item.name) : item.value;
    const meta=document.createElement('div'); meta.className='analysis-meta';
    meta.textContent=analysis.type==='symbols' ? `${item.kind} · ${item.definitions.length} defs · ${item.reads.length+item.writes.length+item.calls.length} refs` : `${item.references.length} references`;
    row.append(name,meta); row.addEventListener('click',() => { analysis.selected=item; renderAnalysisList(); renderAnalysisDetail(item); }); analysisList.appendChild(row);
  });
}
function jumpToSource(sid) {
  historyStack.push({view:activeView,top:0}); switchView('source');
  requestAnimationFrame(() => { const target=document.getElementById('stmt-'+sid); target?.scrollIntoView({behavior:'smooth',block:'center'}); target?.closest('.statement-unit')?.classList.add('flash'); });
}
function appendXrefGroup(container,title,references) {
  if (!references?.length) return; const group=document.createElement('section'); group.className='xref-group';
  const heading=document.createElement('div'); heading.className='xref-title'; heading.textContent=title;
  const list=document.createElement('div'); list.className='xref-list';
  references.forEach(sid => { const button=document.createElement('button'); button.className='xref-link'+(project.user.bookmarks.includes(sid)?' bookmarked':''); button.title='Click to open Source · right-click to toggle bookmark'; button.textContent='@'+sid+'  '+(statementText[sid] || ''); button.addEventListener('click',() => jumpToSource(sid)); button.addEventListener('contextmenu',event => { event.preventDefault(); const index=project.user.bookmarks.indexOf(sid); if (index>=0) project.user.bookmarks.splice(index,1); else project.user.bookmarks.push(sid); saveProjectLocal(); button.classList.toggle('bookmarked',index<0); }); list.appendChild(button); });
  group.append(heading,list); container.appendChild(group);
}
function renderAnalysisDetail(item) {
  analysisDetail.textContent=''; const heading=document.createElement('h2'); heading.className='analysis-heading'; heading.textContent=item.name ? renamed(item.name) : item.value;
  const kind=document.createElement('div'); kind.className='analysis-kind'; kind.textContent=analysis.type==='symbols' ? item.kind : 'string literal'; analysisDetail.append(heading,kind);
  if (analysis.type==='symbols') {
    const editor=document.createElement('div'); editor.className='project-editor';
    const renameLabel=document.createElement('label'); renameLabel.textContent='Display name'; const renameInput=document.createElement('input'); renameInput.value=project.user.renames[item.name] || '';
    const commentLabel=document.createElement('label'); commentLabel.textContent='Comment'; const commentInput=document.createElement('textarea'); commentInput.rows=3; commentInput.value=project.user.symbol_comments[item.name] || '';
    const save=document.createElement('button'); save.textContent='Save annotation'; save.addEventListener('click',() => { if (renameInput.value.trim()) project.user.renames[item.name]=renameInput.value.trim(); else delete project.user.renames[item.name]; if (commentInput.value.trim()) project.user.symbol_comments[item.name]=commentInput.value; else delete project.user.symbol_comments[item.name]; saveProjectLocal(); renderAnalysisList(); renderAnalysisDetail(item); });
    editor.append(renameLabel,renameInput,commentLabel,commentInput,save); analysisDetail.appendChild(editor);
    appendXrefGroup(analysisDetail,'Definitions',item.definitions); appendXrefGroup(analysisDetail,'Writes',item.writes);
    appendXrefGroup(analysisDetail,'Reads',item.reads); appendXrefGroup(analysisDetail,'Calls',item.calls);
  } else appendXrefGroup(analysisDetail,'References',item.references);
}
function exportProject() {
  project.analysis={xrefs:xrefData,cfg:graphData}; const blob=new Blob([JSON.stringify(project,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=url; link.download=document.title.replace(/\.[^.]+$/,'')+'.luare.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
}
async function importProject(event) {
  const file=event.target.files?.[0]; if (!file) return;
  try { const imported=JSON.parse(await file.text()); if (!imported.user) throw new Error('missing user project data'); project.user={...project.user,...imported.user}; saveProjectLocal(); renderAnalysisList(); if (analysis.selected) renderAnalysisDetail(analysis.selected); }
  catch (error) { alert('Invalid luaRE project: '+error.message); }
  event.target.value='';
}

const patchState = {initialized:false,storageKey:'luare-patch:'+sourceDigest};
const patchOriginal=document.getElementById('patch-original'), patchDraft=document.getElementById('patch-draft'), patchStatus=document.getElementById('patch-status');
function patchFilename() { return sourceName.replace(/\.lua$/i,'')+'.patched.lua'; }
function updatePatchStatus(message) {
  if (message) { patchStatus.textContent=message; return; }
  const original=originalSource.split(/\\r?\\n/), draft=patchDraft.value.split(/\\r?\\n/), count=Math.max(original.length,draft.length);
  let changed=0; for (let index=0; index<count; index++) if (original[index]!==draft[index]) changed++;
  patchStatus.textContent=changed ? `${changed} changed line${changed===1?'':'s'} · draft saved locally` : 'Unmodified';
}
function applyPatchPreview() {
  const originalLines=originalSource.split(/\\r?\\n/), patchedLines=patchDraft.value.split(/\\r?\\n/), compatible=originalLines.length===patchedLines.length;
  const counts=new Map(); statementInfo.forEach(item => counts.set(item.line,(counts.get(item.line)||0)+1));
  let ambiguous=0;
  statementInfo.forEach(item => {
    const canMap=compatible && counts.get(item.line)===1, text=canMap ? (patchedLines[item.line-1] ?? '').trim() : originalStatementText[item.sid];
    if (!canMap && originalLines[item.line-1]!==patchedLines[item.line-1]) ambiguous++;
    statementText[item.sid]=text;
    for (const prefix of ['stmt-','flow-stmt-']) { const row=document.getElementById(prefix+item.sid),code=row?.querySelector('code'); if (code) { if (text===originalStatementText[item.sid]) code.innerHTML=item.html; else code.textContent=text; } }
    const debugCode=debugSource.querySelector(`.debug-source-row[data-sid="${item.sid}"] .debug-code`); if (debugCode) { if (text===originalStatementText[item.sid]) debugCode.innerHTML=item.html; else debugCode.textContent=text; }
  });
  graphData.nodes.forEach(node => { if (node.statements) node.text=node.statements.map(sid => statementText[sid] || '').join('\\n'); });
  if (graph.initialized) { renderGraphNodes(); drawEdges(); }
  if (trace.initialized) renderTraceList();
  const modified=patchDraft.value!==originalSource, note=document.getElementById('patch-note');
  note.textContent=!modified?'Browser-only draft · current debug process is unchanged':!compatible?'Preview unavailable after adding/removing lines · download and re-analyze':ambiguous?'Some multi-statement lines remain original · CFG/XREF still use original analysis':'Source/Flow/Graph preview updated · CFG/XREF still use original analysis';
}
function persistPatch() { try { if (patchDraft.value===originalSource) localStorage.removeItem(patchState.storageKey); else localStorage.setItem(patchState.storageKey,patchDraft.value); } catch (_) {} applyPatchPreview(); updatePatchStatus(); }
function initPatch() {
  if (patchState.initialized) return; patchState.initialized=true; patchOriginal.value=originalSource;
  try { patchDraft.value=localStorage.getItem(patchState.storageKey) ?? originalSource; } catch (_) { patchDraft.value=originalSource; }
  patchDraft.addEventListener('input',persistPatch);
  patchDraft.addEventListener('keydown',event => {
    if (event.key==='Tab') { event.preventDefault(); const start=patchDraft.selectionStart,end=patchDraft.selectionEnd; patchDraft.setRangeText('  ',start,end,'end'); persistPatch(); }
    else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase()==='s') { event.preventDefault(); downloadPatch(); }
  });
  const sync=(source,target) => { target.scrollTop=source.scrollTop; target.scrollLeft=source.scrollLeft; };
  patchOriginal.addEventListener('scroll',() => sync(patchOriginal,patchDraft)); patchDraft.addEventListener('scroll',() => sync(patchDraft,patchOriginal));
  document.getElementById('patch-save').addEventListener('click',downloadPatch);
  document.getElementById('patch-reset').addEventListener('click',() => { if (patchDraft.value!==originalSource && !confirm('Discard the browser patch draft?')) return; patchDraft.value=originalSource; persistPatch(); });
  document.getElementById('patch-copy').addEventListener('click',async () => { try { await navigator.clipboard.writeText(patchDraft.value); updatePatchStatus('Copied patched source'); setTimeout(updatePatchStatus,1200); } catch (_) { updatePatchStatus('Clipboard permission denied'); } });
  applyPatchPreview(); updatePatchStatus();
}
function downloadPatch() {
  const blob=new Blob([patchDraft.value],{type:'text/x-lua;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=url; link.download=patchFilename(); link.click(); setTimeout(() => URL.revokeObjectURL(url),1000); updatePatchStatus('Downloaded '+patchFilename()); setTimeout(updatePatchStatus,1400);
}

const traceSession = traceData || {events:[],console:[],stderr:[],exit_code:null};
const trace = {initialized:false,index:0,events:traceSession.events || [],lastLiveCount:0,polling:false,cursorLine:null,breakpoints:new Set(),liveSnapshot:null,liveEditable:false,waitingInput:false,exited:false};
const traceEvents=document.getElementById('trace-events'), debugInspector=document.getElementById('debug-inspector'), debugSource=document.getElementById('debug-source');
function eventSid(event) { return event?.sid || event?.statements?.[0] || null; }
function sourceRowsAtLine(line) { return [...debugSource.querySelectorAll(`.debug-source-row[data-line="${line}"]`)]; }
function selectDebugSource(row) {
  debugSource.querySelectorAll('.cursor').forEach(element => element.classList.remove('cursor'));
  row.classList.add('cursor'); trace.cursorLine=Number(row.dataset.line);
  document.getElementById('live-break-line').value=trace.cursorLine;
}
function renderDebugSource() {
  debugSource.textContent='';
  if (!statementInfo.length) { const empty=document.createElement('div'); empty.className='debug-empty'; empty.textContent='No source statements.'; debugSource.appendChild(empty); return; }
  statementInfo.forEach(item => {
    const row=document.createElement('div'); row.className='debug-source-row'; row.dataset.sid=item.sid; row.dataset.line=item.line;
    const breakpoint=document.createElement('button'); breakpoint.className='debug-breakpoint'; breakpoint.title=liveDebug?'Toggle breakpoint (F9)':'Breakpoints require a live debug session'; breakpoint.disabled=!liveDebug;
    const line=document.createElement('span'); line.className='debug-line'; line.textContent=item.line;
    const code=document.createElement('code'); code.className='debug-code'; if (statementText[item.sid]===originalStatementText[item.sid]) code.innerHTML=item.html; else code.textContent=statementText[item.sid];
    row.append(breakpoint,line,code);
    row.addEventListener('click',() => selectDebugSource(row));
    row.addEventListener('dblclick',event => { if (liveDebug && !event.target.closest('.debug-breakpoint')) toggleBreakpoint(item.line); });
    breakpoint.addEventListener('click',event => { event.stopPropagation(); selectDebugSource(row); toggleBreakpoint(item.line); });
    debugSource.appendChild(row);
  });
}
function snapshotText(snapshot) {
  if (!snapshot) return 'nil';
  if (snapshot.type==='string') return JSON.stringify(snapshot.value)+(snapshot.clipped?' …':'');
  if (snapshot.type==='number' || snapshot.type==='boolean' || snapshot.type==='nil') return String(snapshot.value ?? 'nil');
  if (snapshot.type==='function') return `function ${snapshot.name || '<anonymous>'} @ ${snapshot.source || '?'}:${snapshot.line ?? '?'}`;
  if (snapshot.type==='thread') return `thread (${snapshot.status || '?'})`;
  if (snapshot.type==='table') {
    if (snapshot.recursive) return '{<recursive>}'
    const entries=(snapshot.entries || []).map(entry => `[${snapshotText(entry.key)}] = ${snapshotText(entry.value)}`);
    return '{ '+entries.join(', ')+(snapshot.truncated ? ', …' : '')+' }';
  }
  return `<${snapshot.type || 'unknown'}>`;
}
function makePanel(title,wide=false) {
  const panel=document.createElement('section'); panel.className='inspect-panel'+(wide?' wide':'');
  const heading=document.createElement('div'); heading.className='inspect-title'; heading.textContent=title;
  const body=document.createElement('div'); body.className='inspect-body'; panel.append(heading,body); return {panel,body};
}
function renderValues(title,items,scope,allowEdit=false) {
  const section=makePanel(title);
  if (!items?.length) { const row=document.createElement('div'); row.className='value-row'; row.textContent='No values captured'; section.body.appendChild(row); }
  else items.forEach(item => {
    const editable=allowEdit && Number.isInteger(item.index) && (scope==='local' || scope==='upvalue');
    const row=document.createElement('div'); row.className='value-row'+(editable?' editable':'');
    const name=document.createElement('span'); name.className='value-name'; name.textContent=item.name;
    const value=document.createElement('span'); value.className='value-data'; value.textContent=snapshotText(item.value);
    row.append(name,value);
    if (editable) { const edit=document.createElement('button'); edit.className='value-edit'; edit.title=`Edit ${scope} ${item.name}`; edit.textContent='Edit'; edit.addEventListener('click',() => editPausedValue(scope,item)); row.appendChild(edit); }
    section.body.appendChild(row);
  });
  return section.panel;
}
function encodeUtf8Hex(text) { return [...new TextEncoder().encode(text)].map(byte => byte.toString(16).padStart(2,'0')).join(''); }
function editPausedValue(scope,item) {
  if (!trace.liveEditable) return;
  const expression=prompt(`Set ${scope} ${item.name} to a Lua literal/expression:`,snapshotText(item.value));
  if (expression===null || !expression.trim()) return;
  sendLiveCommand(`set${scope} ${item.index} lua ${encodeUtf8Hex(expression)}`);
}
function markTraceStatement(event) {
  document.querySelectorAll('.runtime-current').forEach(element => element.classList.remove('runtime-current'));
  debugSource.querySelectorAll('.current').forEach(element => element.classList.remove('current'));
  for (const sid of event?.statements || (event?.sid ? [event.sid] : [])) {
    for (const prefix of ['stmt-','flow-stmt-']) document.getElementById(prefix+sid)?.closest('.statement-unit')?.classList.add('runtime-current');
    debugSource.querySelector(`.debug-source-row[data-sid="${sid}"]`)?.classList.add('current');
  }
  if (!eventSid(event) && event?.line) sourceRowsAtLine(event.line).forEach(row => row.classList.add('current'));
  const current=debugSource.querySelector('.current');
  if (current && trace.cursorLine===null) selectDebugSource(current);
  if (current && activeView==='debug') current.scrollIntoView({block:'nearest'});
}
function renderTraceList() {
  traceEvents.textContent='';
  if (!trace.events.length) {
    const empty=document.createElement('div'); empty.className='debug-empty'; empty.textContent='No runtime trace loaded. Run luaRE with --trace --html to record execution.'; traceEvents.appendChild(empty); return;
  }
  const start=Math.max(0,Math.min(trace.index-100,trace.events.length-220));
  const end=Math.min(trace.events.length,start+220);
  for (let index=start; index<end; index++) {
    const event=trace.events[index], row=document.createElement('div'); row.className='trace-event'+(index===trace.index?' selected':''); row.dataset.index=index;
    const seq=document.createElement('span'); seq.className='trace-seq'; seq.textContent='#'+(event.seq ?? index+1);
    const kind=document.createElement('span'); kind.className='trace-kind'; kind.textContent=event.event || 'event';
    const location=document.createElement('span'); location.className='trace-location'; location.textContent=event.sid ? '@'+event.sid : event.line ? 'line '+event.line : '—';
    const code=document.createElement('span'); code.className='trace-code'; code.textContent=statementText[eventSid(event)] || event.message || event.function_name || event.target || '';
    row.append(seq,kind,location,code); row.addEventListener('click',() => selectTrace(index)); row.addEventListener('dblclick',showTraceSource); traceEvents.appendChild(row);
  }
  traceEvents.querySelector('.selected')?.scrollIntoView({block:'nearest'});
}
function renderTraceInspector(event) {
  debugInspector.textContent='';
  if (!event) { const empty=document.createElement('div'); empty.className='debug-empty'; empty.textContent='No event selected.'; debugInspector.appendChild(empty); return; }
  const grid=document.createElement('div'); grid.className='inspect-grid';
  const snapshot=liveDebug && trace.liveSnapshot ? trace.liveSnapshot : event, editable=liveDebug && trace.liveEditable;
  const state=document.createElement('div'); state.className='variables-state'+(editable?'':' readonly');
  state.textContent=editable ? `Current frame @ line ${snapshot?.line ?? '?'} · Locals and upvalues can be changed with Edit` : trace.waitingInput ? 'Target is waiting inside io.read(); submit program input before editing the next paused frame' : liveDebug ? 'Variables are read-only while the target is running or after it exits' : 'Recorded snapshots are read-only';
  grid.appendChild(state);
  grid.append(renderValues(editable?'Current Locals · editable':'Locals',snapshot?.locals,'local',editable),renderValues(editable?'Current Upvalues · editable':'Upvalues',snapshot?.upvalues,'upvalue',editable));
  const meta=makePanel('Event',true), list=document.createElement('dl'); list.className='event-meta';
  [['Event',event.event],['Sequence',event.seq],['Statement',event.sid || event.statements?.join(', ')],['Source line',event.line],['Function',event.function_name],['Call depth',event.depth],['Message',event.message]].forEach(([key,value]) => {
    if (value===undefined || value===null) return; const dt=document.createElement('dt'),dd=document.createElement('dd'); dt.textContent=key; dd.textContent=String(value); list.append(dt,dd);
  });
  meta.body.appendChild(list); grid.appendChild(meta.panel);
  const stack=makePanel('Call Stack',true);
  if (!snapshot?.stack?.length) { const row=document.createElement('div'); row.className='stack-row'; row.textContent='No stack captured'; stack.body.appendChild(row); }
  else snapshot.stack.forEach((frame,index) => { const row=document.createElement('div'); row.className='stack-row'; const number=document.createElement('span'),name=document.createElement('span'),line=document.createElement('span'); number.textContent='#'+index; name.textContent=frame.name || '<anonymous>'; line.className='stack-line'; line.textContent='line '+frame.line; row.append(number,name,line); stack.body.appendChild(row); });
  grid.appendChild(stack.panel);
  const output=makePanel('Console / stderr',true), lines=[...(traceSession.console || []),...(traceSession.stderr || []).map(line => '[stderr] '+line)];
  if (!lines.length) { const row=document.createElement('div'); row.className='console-row'; row.textContent='No output'; output.body.appendChild(row); }
  else lines.forEach(text => { const row=document.createElement('div'); row.className='console-row'; row.textContent=text; output.body.appendChild(row); });
  grid.appendChild(output.panel); debugInspector.appendChild(grid);
}
function selectTrace(index) {
  if (!trace.events.length) return; trace.index=Math.max(0,Math.min(index,trace.events.length-1)); const event=trace.events[trace.index];
  document.getElementById('trace-position').textContent=`${trace.index+1} / ${trace.events.length}`;
  document.getElementById('trace-prev').disabled=trace.index===0; document.getElementById('trace-first').disabled=trace.index===0;
  document.getElementById('trace-next').disabled=trace.index===trace.events.length-1; document.getElementById('trace-last').disabled=trace.index===trace.events.length-1;
  renderTraceList(); renderTraceInspector(event); markTraceStatement(event);
}
function initTrace() {
  if (trace.initialized) return; trace.initialized=true;
  renderDebugSource();
  const lines=trace.events.filter(event => event.event==='line').length;
  document.getElementById('trace-summary').textContent=(traceData || liveDebug) ? `${lines} line events · ${liveDebug ? 'live session' : 'exit '+(traceSession.exit_code ?? 'timeout')}` : 'No trace loaded';
  selectTrace(trace.events.findIndex(event => event.event==='line') >= 0 ? trace.events.findIndex(event => event.event==='line') : 0);
  if (!trace.events.length) { renderTraceList(); renderTraceInspector(null); ['trace-first','trace-prev','trace-next','trace-last','trace-source'].forEach(id => document.getElementById(id).disabled=true); }
  if (liveDebug) { document.querySelectorAll('.live-only').forEach(element => element.hidden=false); pollLiveState(); }
}
function showTraceSource() {
  const sid=eventSid(trace.events[trace.index]); if (!sid) return; historyStack.push({view:'debug',top:0}); switchView('source');
  requestAnimationFrame(() => document.getElementById('stmt-'+sid)?.scrollIntoView({behavior:'smooth',block:'center'}));
}
document.getElementById('trace-first').addEventListener('click',() => selectTrace(0));
document.getElementById('trace-prev').addEventListener('click',() => selectTrace(trace.index-1));
document.getElementById('trace-next').addEventListener('click',() => selectTrace(trace.index+1));
document.getElementById('trace-last').addEventListener('click',() => selectTrace(trace.events.length-1));
document.getElementById('trace-source').addEventListener('click',showTraceSource);
document.addEventListener('keydown',event => { if (activeView==='debug' && event.key==='ArrowUp') { event.preventDefault(); selectTrace(trace.index-1); } else if (activeView==='debug' && event.key==='ArrowDown') { event.preventDefault(); selectTrace(trace.index+1); } });

function setLiveControls(running,exited,waitingInput=false) {
  ['live-step','live-over','live-out','live-runto','live-continue','live-break'].forEach(id => document.getElementById(id).disabled=running || exited);
  if (waitingInput) ['live-step','live-over','live-out','live-runto','live-continue','live-break'].forEach(id => document.getElementById(id).disabled=true);
  document.getElementById('live-break-line').disabled=running || exited;
  document.getElementById('live-input').disabled=!waitingInput || exited;
  document.getElementById('live-input-send').disabled=!waitingInput || exited;
  document.getElementById('live-restart').disabled=running;
  document.getElementById('live-status').textContent=exited ? 'Exited' : waitingInput ? 'Waiting for input' : running ? 'Running…' : 'Paused';
}
async function pollLiveState() {
  if (!liveDebug || trace.polling) return; trace.polling=true;
  try {
    const response=await fetch('/api/state',{cache:'no-store'}), state=await response.json();
    traceSession.events=state.events || []; traceSession.console=state.console || []; traceSession.stderr=state.stderr || []; trace.events=traceSession.events;
    const exited=state.process_exit!==null || state.current?.event==='session_end' || state.current?.event==='error',waitingInput=!state.running && state.current?.event==='input_request';
    trace.liveSnapshot=state.last_pause || trace.liveSnapshot; trace.waitingInput=waitingInput; trace.exited=exited; trace.liveEditable=!state.running && !waitingInput && !exited && !!trace.liveSnapshot;
    const changed=trace.events.length!==trace.lastLiveCount; trace.lastLiveCount=trace.events.length;
    if (changed && trace.events.length) selectTrace(trace.events.length-1);
    else if (!trace.events.length) { renderTraceList(); renderTraceInspector(null); }
    else renderTraceInspector(trace.events[trace.index]);
    setLiveControls(state.running,exited,waitingInput);
    if (waitingInput && document.activeElement!==document.getElementById('live-input')) document.getElementById('live-input').focus();
    if (state.error) document.getElementById('live-status').textContent=state.error;
    const lines=trace.events.filter(event => event.event==='line').length; document.getElementById('trace-summary').textContent=`${lines} line events · ${state.running?'running':exited?'stopped':'paused'}`;
  } catch (error) {
    document.getElementById('live-status').textContent='Disconnected';
  } finally {
    trace.polling=false; setTimeout(pollLiveState,250);
  }
}
async function sendLiveCommand(command) {
  if (!liveDebug) return; trace.liveEditable=false; renderTraceInspector(trace.events[trace.index]); setLiveControls(true,false);
  try {
    const response=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command})});
    if (!response.ok) { const result=await response.json(); throw new Error(result.error || 'command rejected'); }
    return true;
  } catch (error) {
    document.getElementById('live-status').textContent=error.message;
    return false;
  }
}
async function toggleBreakpoint(line=trace.cursorLine) {
  if (!liveDebug || !line) return;
  const enabled=!trace.breakpoints.has(line), command=(enabled?'break ':'clear ')+line;
  if (!await sendLiveCommand(command)) return;
  if (enabled) trace.breakpoints.add(line); else trace.breakpoints.delete(line);
  sourceRowsAtLine(line).forEach(row => row.classList.toggle('has-breakpoint',enabled));
}
function runToCursor() { if (trace.cursorLine) sendLiveCommand('runto '+trace.cursorLine); }
function restartDebug() {
  trace.liveSnapshot=null; trace.liveEditable=false; trace.waitingInput=false; trace.exited=false; trace.lastLiveCount=0;
  sendLiveCommand('restart');
}
function sendProgramInput() {
  const field=document.getElementById('live-input'), encoded=encodeUtf8Hex(field.value);
  sendLiveCommand('input '+(encoded || '-')); field.value='';
}
document.getElementById('live-restart').addEventListener('click',restartDebug);
document.getElementById('live-step').addEventListener('click',() => sendLiveCommand('step'));
document.getElementById('live-over').addEventListener('click',() => sendLiveCommand('over'));
document.getElementById('live-out').addEventListener('click',() => sendLiveCommand('out'));
document.getElementById('live-runto').addEventListener('click',runToCursor);
document.getElementById('live-continue').addEventListener('click',() => sendLiveCommand('continue'));
document.getElementById('live-break').addEventListener('click',() => { const line=document.getElementById('live-break-line').valueAsNumber; if (line>0) toggleBreakpoint(line); });
document.getElementById('live-input-send').addEventListener('click',sendProgramInput);
document.getElementById('live-input').addEventListener('keydown',event => { if (event.key==='Enter') { event.preventDefault(); sendProgramInput(); } });
document.addEventListener('keydown',event => {
  if (!liveDebug || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
  const command=event.key==='F2'?'restart':event.key==='F7'?'step':event.key==='F8'?'over':event.key==='F5'?'continue':event.key==='F4'?'runto':event.key==='F9'?'breakpoint':(event.shiftKey&&event.key==='F11')?'out':null;
  if (!command) return; event.preventDefault(); if (activeView!=='debug') switchView('debug');
  if (command==='restart') restartDebug(); else if (command==='runto') runToCursor(); else if (command==='breakpoint') toggleBreakpoint(); else sendLiveCommand(command);
});

const graph = {initialized:false,scale:1,panX:30,panY:30,page:0,pageSize:36,scopeNodes:[],positions:new Map(),visibleNodes:[],visibleEdges:[],drag:null,pan:null};
const viewport=document.getElementById('graph-view'), world=document.getElementById('graph-world');
const nodesLayer=document.getElementById('graph-nodes'), edgeSvg=document.getElementById('graph-edges');
const scopeSelect=document.getElementById('scope-select');
function numericId(id) { return parseInt(id.slice(2),16); }
function edgeClass(label) { return label.startsWith('call ') ? 'call' : label.replace(/[^a-z]/gi,'-').toLowerCase(); }

function initGraph() {
  if (graph.initialized) return; graph.initialized=true;
  const scopes=[...new Set(graphData.nodes.map(node => node.scope))].sort((a,b) => a==='<main>'?-1:b==='<main>'?1:a.localeCompare(b));
  [{value:'<main>',label:'Main'},{value:'*',label:'All scopes'},...scopes.filter(scope => scope!=='<main>').map(scope => ({value:scope,label:scope}))]
    .forEach(item => { const option=document.createElement('option'); option.value=item.value; option.textContent=item.label; scopeSelect.appendChild(option); });
  scopeSelect.value='<main>'; scopeSelect.addEventListener('change',() => { graph.page=0; rebuildGraph(); }); rebuildGraph();
}
function rebuildGraph() {
  const scope=scopeSelect.value;
  graph.scopeNodes=graphData.nodes.filter(node => scope==='*' || node.scope===scope).sort((a,b) => numericId(a.id)-numericId(b.id));
  const pageCount=Math.max(1,Math.ceil(graph.scopeNodes.length/graph.pageSize)); graph.page=Math.max(0,Math.min(graph.page,pageCount-1));
  const pageStart=graph.page*graph.pageSize, pageEnd=Math.min(pageStart+graph.pageSize,graph.scopeNodes.length);
  graph.visibleNodes=graph.scopeNodes.slice(pageStart,pageEnd);
  const ids=new Set(graph.visibleNodes.map(node => node.id));
  graph.visibleEdges=graphData.edges.filter(edge => ids.has(edge.from) && ids.has(edge.to));
  graph.positions.clear(); layoutGraph(); renderGraphNodes(); drawEdges();
  document.getElementById('graph-page').textContent=`${graph.page+1} / ${pageCount}`;
  document.getElementById('graph-prev').disabled=graph.page===0;
  document.getElementById('graph-next').disabled=graph.page>=pageCount-1;
  const range=graph.scopeNodes.length ? `${pageStart+1}–${pageEnd} / ${graph.scopeNodes.length}` : '0 / 0';
  document.getElementById('graph-status').textContent=`${range} nodes · ${graph.visibleEdges.length} edges`;
  requestAnimationFrame(fitGraph);
}
function layoutGraph() {
  const depth=new Map(graph.visibleNodes.map(node => [node.id,0]));
  const outgoing=new Map(graph.visibleNodes.map(node => [node.id,[]]));
  graph.visibleEdges.forEach(edge => outgoing.get(edge.from)?.push(edge));
  graph.visibleNodes.forEach(node => (outgoing.get(node.id)||[]).forEach(edge => {
    if (numericId(edge.to)>numericId(edge.from)) depth.set(edge.to,Math.max(depth.get(edge.to),depth.get(edge.from)+1));
  }));
  const rows=new Map();
  graph.visibleNodes.forEach(node => { const rank=depth.get(node.id); if (!rows.has(rank)) rows.set(rank,[]); rows.get(rank).push(node); });
  [...rows.entries()].sort((a,b) => a[0]-b[0]).forEach(([rank,nodes]) => nodes.forEach((node,column) => graph.positions.set(node.id,{x:40+column*320,y:40+rank*135})));
}
function renderGraphNodes() {
  nodesLayer.textContent=''; const controls=new Set(['if','elseif','else','while','for','repeat','until']);
  graph.visibleNodes.forEach(node => {
    const position=graph.positions.get(node.id), element=document.createElement('div');
    element.className='graph-node'+(controls.has(node.kind)?' control':'')+(node.kind==='terminal'?' terminal':''); element.dataset.id=node.id;
    element.style.transform=`translate(${position.x}px,${position.y}px)`;
    const head=document.createElement('div'); head.className='node-head';
    const anchor=document.createElement('span'); anchor.textContent='@'+node.id;
    const line=document.createElement('span'); line.className='node-line'; line.textContent='src:'+node.line+(node.end_line!==node.line?'–'+node.end_line:''); head.append(anchor,line);
    const code=document.createElement('div'); code.className='node-code'; code.textContent=node.text; element.append(head,code);
    element.addEventListener('pointerdown',startNodeDrag); nodesLayer.appendChild(element);
  });
}
function drawEdges() {
  edgeSvg.querySelectorAll('.graph-edge,.edge-label').forEach(element => element.remove());
  const byId=new Map([...nodesLayer.children].map(element => [element.dataset.id,element])), ns='http://www.w3.org/2000/svg';
  graph.visibleEdges.forEach(edge => {
    const source=byId.get(edge.from), target=byId.get(edge.to); if (!source || !target) return;
    const a=graph.positions.get(edge.from), b=graph.positions.get(edge.to);
    const sx=a.x+source.offsetWidth/2, sy=a.y+source.offsetHeight, tx=b.x+target.offsetWidth/2, ty=b.y;
    const bend=Math.max(45,Math.abs(ty-sy)*.45), path=document.createElementNS(ns,'path');
    path.setAttribute('d',`M ${sx} ${sy} C ${sx} ${sy+bend}, ${tx} ${ty-bend}, ${tx} ${ty}`); path.setAttribute('class','graph-edge '+edgeClass(edge.label)); edgeSvg.appendChild(path);
    const label=document.createElementNS(ns,'text'); label.setAttribute('x',String((sx+tx)/2+5)); label.setAttribute('y',String((sy+ty)/2-5));
    label.setAttribute('class','edge-label'); label.textContent=edge.label; edgeSvg.appendChild(label);
  });
}
function applyTransform() { world.style.transform=`translate(${graph.panX}px,${graph.panY}px) scale(${graph.scale})`; }
function setZoom(next,clientX=viewport.clientWidth/2,clientY=viewport.clientHeight/2) {
  next=Math.max(.15,Math.min(2.5,next)); const rect=viewport.getBoundingClientRect(), x=clientX-rect.left, y=clientY-rect.top;
  const wx=(x-graph.panX)/graph.scale, wy=(y-graph.panY)/graph.scale; graph.panX=x-wx*next; graph.panY=y-wy*next; graph.scale=next; applyTransform();
}
function fitGraph() {
  if (!graph.visibleNodes.length) return; const elements=[...nodesLayer.children];
  const bounds=elements.reduce((box,element) => { const p=graph.positions.get(element.dataset.id); return {minX:Math.min(box.minX,p.x),minY:Math.min(box.minY,p.y),maxX:Math.max(box.maxX,p.x+element.offsetWidth),maxY:Math.max(box.maxY,p.y+element.offsetHeight)}; },{minX:Infinity,minY:Infinity,maxX:-Infinity,maxY:-Infinity});
  const padding=70,width=bounds.maxX-bounds.minX,height=bounds.maxY-bounds.minY;
  graph.scale=Math.max(.15,Math.min(1.25,(viewport.clientWidth-padding*2)/width,(viewport.clientHeight-padding*2)/height));
  graph.panX=(viewport.clientWidth-width*graph.scale)/2-bounds.minX*graph.scale; graph.panY=(viewport.clientHeight-height*graph.scale)/2-bounds.minY*graph.scale; applyTransform();
}
function startNodeDrag(event) {
  event.stopPropagation(); const element=event.currentTarget,id=element.dataset.id,position=graph.positions.get(id);
  graph.drag={element,id,startX:event.clientX,startY:event.clientY,x:position.x,y:position.y,moved:false}; element.setPointerCapture(event.pointerId);
  element.addEventListener('pointermove',moveNode); element.addEventListener('pointerup',endNodeDrag,{once:true});
}
function moveNode(event) {
  if (!graph.drag) return; const dx=(event.clientX-graph.drag.startX)/graph.scale,dy=(event.clientY-graph.drag.startY)/graph.scale;
  graph.drag.moved ||= Math.abs(dx)+Math.abs(dy)>4; const position={x:graph.drag.x+dx,y:graph.drag.y+dy}; graph.positions.set(graph.drag.id,position);
  graph.drag.element.style.transform=`translate(${position.x}px,${position.y}px)`; drawEdges();
}
function endNodeDrag() {
  const drag=graph.drag; if (!drag) return; drag.element.removeEventListener('pointermove',moveNode); graph.drag=null;
  if (!drag.moved) { historyStack.push({view:'graph',top:0}); switchView('source'); requestAnimationFrame(() => document.getElementById('stmt-'+drag.id)?.scrollIntoView({block:'center'})); }
}
viewport.addEventListener('pointerdown',event => { if (event.target.closest('.graph-node') || event.target.closest('#graph-tools')) return; graph.pan={x:event.clientX,y:event.clientY,panX:graph.panX,panY:graph.panY}; viewport.setPointerCapture(event.pointerId); });
viewport.addEventListener('pointermove',event => { if (!graph.pan) return; graph.panX=graph.pan.panX+event.clientX-graph.pan.x; graph.panY=graph.pan.panY+event.clientY-graph.pan.y; applyTransform(); });
viewport.addEventListener('pointerup',() => { graph.pan=null; });
viewport.addEventListener('wheel',event => { event.preventDefault(); setZoom(graph.scale*(event.deltaY<0?1.12:.89),event.clientX,event.clientY); },{passive:false});
document.getElementById('zoom-in').addEventListener('click',() => setZoom(graph.scale*1.2));
document.getElementById('zoom-out').addEventListener('click',() => setZoom(graph.scale/1.2));
document.getElementById('graph-prev').addEventListener('click',() => { if (graph.page>0) { graph.page--; rebuildGraph(); } });
document.getElementById('graph-next').addEventListener('click',() => { if ((graph.page+1)*graph.pageSize<graph.scopeNodes.length) { graph.page++; rebuildGraph(); } });
document.getElementById('fit-graph').addEventListener('click',fitGraph);
document.getElementById('reset-graph').addEventListener('click',rebuildGraph);
initPatch();
if (liveDebug) switchView('debug');
</script></body></html>'''
    return (
        page.replace("__TITLE__", html.escape(title))
        .replace("__SOURCE_ROWS__", "".join(source_rows))
        .replace("__FLOW_ROWS__", "".join(runtime_rows))
        .replace("__SYMBOL_MAP__", symbol_map)
        .replace("__CFG_JSON__", cfg_json)
        .replace("__XREF_JSON__", xref_json)
        .replace("__TRACE_JSON__", trace_json)
        .replace("__STATEMENT_TEXT_JSON__", statement_text_json)
        .replace("__STATEMENT_INFO_JSON__", statement_info_json)
        .replace("__SOURCE_TEXT_JSON__", source_text_json)
        .replace("__SOURCE_NAME_JSON__", source_name_json)
        .replace("__SOURCE_DIGEST_JSON__", source_digest_json)
        .replace("__LIVE_DEBUG__", live_debug_json)
        .replace("__PROJECT_JSON__", project_json)
    )


def collect_trace(
    inp: Path,
    stmts: list[Statement],
    lua_executable: str = "lua",
    timeout: float = 10.0,
    max_events: int = 20000,
    script_args: list[str] | None = None,
) -> dict:
    """Execute a Lua file under the trace hook and map line events to @ anchors.

    This intentionally runs in a separate process but is not a security
    sandbox. Callers should use an OS/container sandbox for untrusted input.
    """
    runner = Path(__file__).with_name("luare_trace.lua").resolve()
    target = inp.resolve()
    command = [lua_executable, str(runner), str(target), str(max_events), *(script_args or [])]
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=str(inp.parent.resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        exit_code = None

    line_map: dict[int, list[str]] = {}
    for st in stmts:
        if st.kind != "comment":
            line_map.setdefault(st.src_line, []).append(st.sid)

    prefix = "@@LUARE@@"
    events = []
    console = []
    malformed = []
    for line in stdout.splitlines():
        marker = line.find(prefix)
        if marker < 0:
            console.append(line)
            continue
        if marker:
            console.append(line[:marker])
        try:
            event = json.loads(line[marker + len(prefix):])
        except json.JSONDecodeError:
            malformed.append(line[marker + len(prefix):])
            continue
        source_line = event.get("line")
        candidates = line_map.get(source_line, []) if isinstance(source_line, int) else []
        if candidates:
            event["statements"] = candidates
            if len(candidates) == 1:
                event["sid"] = candidates[0]
        events.append(event)

    if timed_out:
        events.append({"event": "timeout", "message": f"execution exceeded {timeout:g}s"})
    return {
        "version": 1,
        "target": str(target),
        "lua": lua_executable,
        "timeout": timeout,
        "max_events": max_events,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "events": events,
        "console": console,
        "stderr": stderr.splitlines(),
        "malformed_events": malformed,
    }


class LiveDebugger:
    """Line-oriented controller for the Lua hook runner's live mode."""

    def __init__(
        self,
        inp: Path,
        stmts: list[Statement],
        lua_executable: str = "lua",
        max_events: int = 20000,
        script_args: list[str] | None = None,
    ):
        self.inp = inp.resolve()
        self.lua_executable = lua_executable
        self.max_events = max_events
        self.script_args = list(script_args or [])
        self.runner = Path(__file__).with_name("luare_trace.lua").resolve()
        self.line_map: dict[int, list[str]] = {}
        for st in stmts:
            if st.kind != "comment":
                self.line_map.setdefault(st.src_line, []).append(st.sid)
        self._spawn()

    def _spawn(self) -> None:
        self.events: list[dict] = []
        self.console: list[str] = []
        self.process = subprocess.Popen(
            [self.lua_executable, str(self.runner), str(self.inp), str(self.max_events), "--luare-live", *self.script_args],
            cwd=str(self.inp.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def restart(self) -> None:
        """Replace the completed/paused target with a fresh process."""
        self.close()
        self._spawn()

    def _map_event(self, event: dict) -> dict:
        source_line = event.get("line")
        candidates = self.line_map.get(source_line, []) if isinstance(source_line, int) else []
        if candidates:
            event["statements"] = candidates
            if len(candidates) == 1:
                event["sid"] = candidates[0]
        return event

    def read_until_stop(self, acknowledgements: set[str] | None = None) -> dict:
        prefix = "@@LUARE@@"
        acknowledgements = acknowledgements or set()
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if line == "":
                return {"event": "process_exit", "exit_code": self.process.poll()}
            line = line.rstrip("\r\n")
            marker = line.find(prefix)
            if marker < 0:
                self.console.append(line)
                continue
            if marker:
                self.console.append(line[:marker])
            try:
                event = self._map_event(json.loads(line[marker + len(prefix):]))
            except json.JSONDecodeError:
                self.console.append(line)
                continue
            self.events.append(event)
            if event.get("paused") or event.get("event") in acknowledgements or event.get("event") in {"input_request", "session_end", "error"}:
                return event

    def command(self, command: str) -> dict:
        if self.process.poll() is not None:
            return {"event": "process_exit", "exit_code": self.process.returncode}
        if not re.match(r"^(?:step|over|out|continue|quit|break\s+\d+|clear\s+\d+|runto\s+\d+|input\s+(?:[0-9A-Fa-f]+|-)|set(?:local|upvalue)\s+\d+\s+lua\s+[0-9A-Fa-f]+)$", command):
            raise ValueError(f"unsupported debugger command: {command}")
        assert self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        acknowledgements = {"breakpoint_set"} if command.startswith("break ") else {"breakpoint_cleared"} if command.startswith("clear ") else set()
        return self.read_until_stop(acknowledgements)

    def trace_data(self) -> dict:
        return {
            "version": 1,
            "mode": "live",
            "target": str(self.inp),
            "lua": self.lua_executable,
            "max_events": self.max_events,
            "exit_code": self.process.poll(),
            "timed_out": False,
            "events": self.events,
            "console": self.console,
            "stderr": [],
            "malformed_events": [],
        }

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                try:
                    if self.events and self.events[-1].get("event") == "session_end":
                        self.process.wait(timeout=2)
                    else:
                        self.command("quit")
                        self.process.wait(timeout=2)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    self.process.terminate()
                    self.process.wait(timeout=2)
        finally:
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class LiveDebugSession:
    """Thread-safe asynchronous facade used by the browser debug server."""

    def __init__(self, debugger: LiveDebugger):
        self.debugger = debugger
        self.lock = threading.Lock()
        self.current: dict | None = None
        self.last_pause: dict | None = None
        self.running = True
        self.error: str | None = None
        self.breakpoints: set[int] = set()
        self.worker = threading.Thread(target=self._wait, daemon=True)
        self.worker.start()

    def _wait(self, command: str | None = None) -> None:
        try:
            event = self.debugger.command(command) if command else self.debugger.read_until_stop()
            with self.lock:
                self.current = event
                if event.get("paused"):
                    self.last_pause = event
                self.running = False
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.running = False

    def _restart(self, breakpoints: list[int]) -> None:
        try:
            self.debugger.restart()
            event = self.debugger.read_until_stop()
            pause = event if event.get("paused") else None
            if pause:
                for line in breakpoints:
                    self.debugger.command(f"break {line}")
            with self.lock:
                self.current = event
                self.last_pause = pause
                self.running = False
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.running = False

    def command(self, command: str) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("target is already running")
            if command == "restart":
                self.running = True
                self.error = None
                breakpoints = sorted(self.breakpoints)
                self.worker = threading.Thread(target=self._restart, args=(breakpoints,), daemon=True)
                self.worker.start()
                return
            if self.debugger.process.poll() is not None:
                raise RuntimeError("target process has exited")
            match = re.match(r"^break\s+(\d+)$", command)
            if match:
                self.breakpoints.add(int(match.group(1)))
            match = re.match(r"^clear\s+(\d+)$", command)
            if match:
                self.breakpoints.discard(int(match.group(1)))
            self.running = True
            self.error = None
        self.worker = threading.Thread(target=self._wait, args=(command,), daemon=True)
        self.worker.start()

    def state(self) -> dict:
        with self.lock:
            trace = self.debugger.trace_data()
            return {
                "running": self.running,
                "current": self.current,
                "last_pause": self.last_pause,
                "error": self.error,
                "process_exit": self.debugger.process.poll(),
                "events": list(trace["events"]),
                "console": list(trace["console"]),
                "stderr": list(trace["stderr"]),
            }

    def close(self) -> None:
        self.debugger.close()


def make_debug_server(
    page: str,
    session: LiveDebugSession,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> http.server.ThreadingHTTPServer:
    """Create the local HTTP bridge used by the live Debug tab."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, status: int, value) -> None:
            self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self.send_json(200, session.state())
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/command":
                self.send_json(404, {"error": "not found"})
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                command = body.get("command", "")
                if not isinstance(command, str):
                    raise ValueError("command must be a string")
                session.command(command)
                self.send_json(202, {"accepted": command})
            except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                self.send_json(409, {"error": str(exc)})

        def log_message(self, format, *args):
            return

    return http.server.ThreadingHTTPServer((host, port), Handler)


def serve_live_debugger(
    page: str,
    debugger: LiveDebugger,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> dict:
    session = LiveDebugSession(debugger)
    server = make_debug_server(page, session, host, port)
    actual_host, actual_port = server.server_address[:2]
    display_host = "127.0.0.1" if actual_host in ("0.0.0.0", "::") else actual_host
    url = f"http://{display_host}:{actual_port}/"
    print(f"luaRE live debugger: {url}")
    print("Press Ctrl+C to stop the server and save the trace.")
    if open_browser:
        opener = threading.Timer(0.2, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session.close()
    return debugger.trace_data()


def run_cli_debugger(debugger: LiveDebugger) -> dict:
    """Run a small terminal frontend for the live debugger protocol."""
    event = debugger.read_until_stop()
    while True:
        kind = event.get("event", "event")
        location = f" line {event['line']}" if event.get("line") else ""
        anchor = f" @{event['sid']}" if event.get("sid") else ""
        function = f" in {event['function_name']}" if event.get("function_name") else ""
        print(f"[{kind}]{anchor}{location}{function}")
        if kind in {"session_end", "error", "process_exit"}:
            if event.get("message"):
                print(event["message"], file=sys.stderr)
            break
        try:
            raw = input("(luaRE) ").strip()
        except EOFError:
            raw = "quit"
        aliases = {"s": "step", "n": "over", "o": "out", "c": "continue", "q": "quit"}
        command = aliases.get(raw, raw)
        if command == "locals":
            for item in event.get("locals", []):
                print(f"  {item['name']} = {item['value']}")
            continue
        if command in {"help", "?", ""}:
            print("s/step  n/over  o/out  c/continue  break N  clear N  locals  q/quit")
            continue
        try:
            event = debugger.command(command)
        except ValueError as exc:
            print(exc)
    return debugger.trace_data()


def analyze_file(
    inp: Path,
    out: Path | None,
    html_out: Path | None,
    trace_data: dict | None = None,
    project_data: dict | None = None,
):
    src = inp.read_text(encoding="utf-8")
    stmts = normalize_source(src)
    meta = annotate(stmts)
    rendered = render_lua(stmts)
    out = out or inp.with_name(inp.stem + ".flow.lua")
    out.write_text(rendered, encoding="utf-8")
    if html_out:
        html_out.write_text(render_html(stmts, meta, inp.name, trace_data, project_data=project_data, source_text=src), encoding="utf-8")
    return out, html_out


def main():
    p = argparse.ArgumentParser(description="Annotate Lua source with navigable flow/call anchors")
    p.add_argument("input", type=Path)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--html", nargs="?", const=True, help="also generate interactive HTML viewer; optional path")
    p.add_argument("--trace", action="store_true", help="execute the input under Lua and record a runtime trace")
    p.add_argument("--debug-cli", action="store_true", help="pause and control the Lua target in an interactive terminal debugger")
    p.add_argument("--debug-web", "--gui", dest="debug_web", action="store_true", help="open the live browser debugger GUI")
    p.add_argument("--no-browser", action="store_true", help="serve --gui without opening the default browser")
    p.add_argument("--host", default="127.0.0.1", help="host used by --gui (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="port used by --gui (default: 8765)")
    p.add_argument("--trace-out", type=Path, help="runtime trace JSON path (default: input.trace.json)")
    p.add_argument("--lua", default="lua", help="Lua interpreter used by dynamic analysis (default: lua)")
    p.add_argument("--trace-timeout", type=float, default=10.0, help="trace process timeout in seconds")
    p.add_argument("--max-events", type=int, default=20000, help="maximum runtime trace events")
    p.add_argument("--script-arg", action="append", default=[], help="argument passed to the traced Lua script; repeatable")
    p.add_argument("--project", nargs="?", const=True, help="create/update a .luare.json analysis project; optional path")
    args = p.parse_args()
    html_out = None
    if args.html:
        html_out = Path(args.html) if isinstance(args.html, str) else args.input.with_name(args.input.stem + ".flow.html")
    project_data = None
    project_path = None
    if args.project:
        project_path = Path(args.project) if isinstance(args.project, str) else args.input.with_name(args.input.stem + ".luare.json")
        existing_project = None
        if project_path.exists():
            try:
                existing_project = json.loads(project_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                p.error(f"invalid project JSON at {project_path}: {exc}")
        project_stmts = normalize_source(args.input.read_text(encoding="utf-8"))
        annotate(project_stmts)
        project_data = build_project(args.input, project_stmts, existing_project)
        project_path.write_text(json.dumps(project_data, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.debug_web:
        if args.trace or args.debug_cli:
            p.error("--debug-web cannot be combined with --trace or --debug-cli")
        print("warning: live debugging executes the target; use an OS/container sandbox for untrusted Lua", file=sys.stderr)
        source = args.input.read_text(encoding="utf-8")
        live_stmts = normalize_source(source)
        live_meta = annotate(live_stmts)
        output = args.output or args.input.with_name(args.input.stem + ".flow.lua")
        html_out = html_out or args.input.with_name(args.input.stem + ".flow.html")
        output.write_text(render_lua(live_stmts), encoding="utf-8")
        live_page = render_html(live_stmts, live_meta, args.input.name, live_debug=True, project_data=project_data, source_text=source)
        html_out.write_text(live_page, encoding="utf-8")
        try:
            with LiveDebugger(
                args.input,
                live_stmts,
                lua_executable=args.lua,
                max_events=args.max_events,
                script_args=args.script_arg,
            ) as debugger:
                trace_data = serve_live_debugger(live_page, debugger, args.host, args.port, not args.no_browser)
        except FileNotFoundError:
            p.error(f"Lua interpreter not found: {args.lua}")
        trace_out = args.trace_out or args.input.with_name(args.input.stem + ".trace.json")
        trace_out.write_text(json.dumps(trace_data, ensure_ascii=False, indent=2), encoding="utf-8")
        html_out.write_text(render_html(live_stmts, live_meta, args.input.name, trace_data, project_data=project_data, source_text=source), encoding="utf-8")
        print(output)
        print(html_out)
        print(trace_out)
        if project_path: print(project_path)
        return
    trace_data = None
    trace_out = None
    if args.trace and args.debug_cli:
        p.error("--trace and --debug-cli cannot be used together")
    if args.trace or args.debug_cli:
        print("warning: dynamic analysis executes the target; use an OS/container sandbox for untrusted Lua", file=sys.stderr)
        trace_stmts = normalize_source(args.input.read_text(encoding="utf-8"))
        annotate(trace_stmts)
        try:
            if args.debug_cli:
                with LiveDebugger(
                    args.input,
                    trace_stmts,
                    lua_executable=args.lua,
                    max_events=args.max_events,
                    script_args=args.script_arg,
                ) as debugger:
                    trace_data = run_cli_debugger(debugger)
            else:
                trace_data = collect_trace(
                    args.input,
                    trace_stmts,
                    lua_executable=args.lua,
                    timeout=args.trace_timeout,
                    max_events=args.max_events,
                    script_args=args.script_arg,
                )
        except FileNotFoundError:
            p.error(f"Lua interpreter not found: {args.lua}")
        trace_out = args.trace_out or args.input.with_name(args.input.stem + ".trace.json")
        trace_out.write_text(json.dumps(trace_data, ensure_ascii=False, indent=2), encoding="utf-8")
    out, hout = analyze_file(args.input, args.output, html_out, trace_data, project_data)
    print(out)
    if hout: print(hout)
    if trace_out: print(trace_out)
    if project_path: print(project_path)

if __name__ == "__main__":
    main()
