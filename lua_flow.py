from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
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


def annotate(stmts: list[Statement]) -> dict:
    # Assign stable statement ids based on position + source.
    for i, st in enumerate(stmts):
        st.sid = f"0x{i+1:06X}"
        st.kind = classify(st.text)

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

    # Structural matching.
    stack: list[dict] = []
    block_meta: dict[int, dict] = {}
    end_to_start: dict[int, int] = {}
    for i, st in enumerate(stmts):
        k = st.kind
        if k in ("if", "while", "for", "function", "do", "repeat"):
            stack.append({"kind": k, "start": i, "arms": [i] if k == "if" else []})
        elif k in ("elseif", "else"):
            # nearest if
            for frame in reversed(stack):
                if frame["kind"] == "if":
                    frame["arms"].append(i); break
        elif k == "until":
            # close repeat
            j = next((p for p in range(len(stack)-1, -1, -1) if stack[p]["kind"] == "repeat"), None)
            if j is not None:
                fr = stack.pop(j); fr["end"] = i; block_meta[fr["start"]] = fr; end_to_start[i] = fr["start"]
        elif k == "end":
            if stack:
                fr = stack.pop(); fr["end"] = i; block_meta[fr["start"]] = fr; end_to_start[i] = fr["start"]

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
    esc = re.sub(r"@(?P<id>0x[0-9A-Fa-f]+)", lambda m: f'<a href="#stmt-{m.group("id")}" class="jump">@{m.group("id")}</a>', esc)
    esc = re.sub(r"\$(?P<id>0x[0-9A-Fa-f]+)", lambda m: f'<a href="#sym-{m.group("id")}" class="jump">${m.group("id")}</a>', esc)
    esc = re.sub(
        r"(?<![\w$@])(?P<id>[ACEL]0x[0-9A-Fa-f]+)",
        lambda m: f'<a href="#nav-{m.group("id")}" class="jump">{m.group("id")}</a>',
        esc,
    )
    return esc


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


def build_cfg(stmts: list[Statement]) -> dict:
    """Build graph-view nodes and edges from resolved flow annotations."""
    nodes = []
    edges = []
    seen: set[tuple[str, str, str]] = set()

    for st in stmts:
        if st.kind == "comment":
            continue
        nodes.append({"id": st.sid, "text": st.text, "line": st.src_line, "kind": st.kind})

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

    return {"nodes": nodes, "edges": edges}


def render_html(stmts: list[Statement], meta: dict, title: str) -> str:
    rows = []
    # figure symbol definition locations
    defs = {}
    for st in stmts:
        name = extract_function_name(st.text)
        if name:
            defs[meta["functions"][name]] = st.sid
    for st in stmts:
        if st.kind == "comment":
            rows.append(f'<div class="line comment"><span class="src">{st.src_line}</span><code>{anchorize_comment(st.text)}</code></div>')
            continue
        sym_attrs = ""
        name = extract_function_name(st.text)
        if name:
            symid = meta["functions"][name]
            sym_attrs = f' data-symbol="{symid}"'
        ann = [f"-- @{st.sid} [src:{st.src_line}]"] + st.annotations
        for a in ann:
            aid = annotation_nav_id(a)
            rows.append(f'<div class="line annotation"{aid}><span class="src"></span><code>{anchorize_comment(a)}</code></div>')
        # Add actual IDs for nav targets
        extra_ids = f' id="stmt-{st.sid}"'
        if name:
            symid = meta["functions"][name]
            extra_ids += f' data-sym-anchor="sym-{symid[1:]}"'
        rows.append(f'<div class="line code"{extra_ids}{sym_attrs}><span class="src">{st.src_line}</span><code>{html.escape(st.text)}</code></div>')
    # Hidden symbol anchors placed before their statement through JS mapping.
    symbol_map = json.dumps({k[1:]: v for k, v in defs.items()})
    return f'''<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; background:#101218; color:#d8dee9; font:14px ui-monospace,SFMono-Regular,Consolas,monospace; }}
#bar {{ position:sticky; top:0; z-index:2; padding:10px 14px; background:#171b24; border-bottom:1px solid #2a3140; }}
#bar b {{ color:#9ccfd8; }} #bar span {{ opacity:.7; margin-left:16px; }}
#code {{ padding:10px 0 40px; }}
.line {{ display:flex; min-height:20px; line-height:20px; white-space:pre-wrap; scroll-margin-top:52px; }}
.line:hover {{ background:#191f2a; }}
.src {{ width:58px; flex:none; text-align:right; padding-right:12px; color:#596579; user-select:none; }}
code {{ flex:1; padding-right:18px; }}
.annotation code, .comment code {{ color:#6f849c; }}
.code code {{ color:#e0def4; }}
a.jump {{ color:#ebbcba; text-decoration:none; border-bottom:1px dotted #ebbcba66; cursor:pointer; }}
a.jump:hover {{ color:#f6c177; }}
.flash {{ animation: flash .8s ease-out; }} @keyframes flash {{ from {{ background:#403b25; }} to {{ background:transparent; }} }}
</style></head>
<body><div id="bar"><b>luare</b><span>click @ / $ to jump · Backspace or Alt+← to go back</span></div><div id="code">{''.join(rows)}</div>
<script>
const symbolMap = {symbol_map};
const historyStack = [];
function targetFor(href) {{
  if (href.startsWith('#sym-')) {{ const id=href.slice(5); const sid=symbolMap[id]; return sid ? document.getElementById('stmt-'+sid) : null; }}
  return document.querySelector(href);
}}
document.addEventListener('click', e => {{
  const a=e.target.closest('a.jump'); if(!a) return; e.preventDefault();
  const target=targetFor(a.getAttribute('href')); if(!target) return;
  historyStack.push(window.scrollY); target.scrollIntoView({{behavior:'smooth', block:'center'}}); target.classList.add('flash'); setTimeout(()=>target.classList.remove('flash'),900);
}});
function goBack() {{ if(!historyStack.length) return; window.scrollTo({{top:historyStack.pop(), behavior:'smooth'}}); }}
document.addEventListener('keydown', e => {{ if(e.key==='Backspace' && !/INPUT|TEXTAREA/.test(e.target.tagName)) {{e.preventDefault(); goBack();}} if(e.altKey && e.key==='ArrowLeft') {{e.preventDefault(); goBack();}} }});
</script></body></html>'''


def analyze_file(inp: Path, out: Path | None, html_out: Path | None):
    src = inp.read_text(encoding="utf-8")
    stmts = normalize_source(src)
    meta = annotate(stmts)
    rendered = render_lua(stmts)
    out = out or inp.with_name(inp.stem + ".flow.lua")
    out.write_text(rendered, encoding="utf-8")
    if html_out:
        html_out.write_text(render_html(stmts, meta, inp.name), encoding="utf-8")
    return out, html_out


def main():
    p = argparse.ArgumentParser(description="Annotate Lua source with navigable flow/call anchors")
    p.add_argument("input", type=Path)
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--html", nargs="?", const=True, help="also generate interactive HTML viewer; optional path")
    args = p.parse_args()
    html_out = None
    if args.html:
        html_out = Path(args.html) if isinstance(args.html, str) else args.input.with_name(args.input.stem + ".flow.html")
    out, hout = analyze_file(args.input, args.output, html_out)
    print(out)
    if hout: print(hout)

if __name__ == "__main__":
    main()
