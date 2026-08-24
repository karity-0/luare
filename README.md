<div align="center">

# luaRE

### A reverse-engineering workbench for Lua

Static flow recovery, Graphs view, XREF analysis, runtime tracing,
live debugging, and non-destructive patch drafting in one local HTML workspace.

</div>

> [!NOTE]
> luaRE is an analysis tool for readable and obfuscated Lua source.

## Highlights

| | Capability | What it provides |
|---|---|---|
| `@` | Execution-aware anchors | Deterministic statement IDs ordered by first expected runtime visitation |
| ◇ | Control-flow graph | Function-scoped, draggable basic blocks with pan, zoom, paging, and source jumps |
| ↔ | XREF analysis | Definitions, reads, writes, calls, strings, renames, comments, and bookmarks |
| ▶ | Live debugger | Breakpoints, step into/over/out, restart, run-to-cursor, locals, upvalues, and call stack |
| ✎ | Runtime editing | Change paused locals and upvalues with Lua literal expressions |
| ⧉ | Patch workspace | Edit a browser-only draft and download it without touching the input file |

luaRE uses only the Python standard library for static analysis and HTML
generation. A Lua interpreter is required only for tracing and live debugging.

## Quick start

Requirements:

- Python 3.10 or newer
- A PUC Lua interpreter for dynamic analysis; current development is tested with Lua 5.3

Generate the annotated Lua file and standalone workbench:

```bash
python lua_flow.py input.lua --html
```

Launch the live debugger GUI:

```bash
python lua_flow.py input.lua --gui
```

The browser opens the local workbench automatically. Keep the terminal process
running while debugging. Use `--no-browser` to print the URL without opening it,
or `--host` / `--port` to change the default `127.0.0.1:8765` endpoint.

> [!WARNING]
> Dynamic analysis executes the target Lua program. luaRE is not a security
> sandbox. Run unknown or hostile samples inside a disposable VM or container.

## Analysis modes

| Command | Mode | Result |
|---|---|---|
| `python lua_flow.py input.lua --html` | Static | Annotated Lua and standalone HTML workbench |
| `python lua_flow.py input.lua --trace --html` | Recorded trace | Executes to completion and embeds replayable runtime events |
| `python lua_flow.py input.lua --debug-cli --html` | Terminal debugger | Pauses and controls the real Lua process from the terminal |
| `python lua_flow.py input.lua --gui` | Live GUI debugger | Serves the workbench with asynchronous process control |
| `python lua_flow.py input.lua --html --project` | Persistent project | Creates or refreshes analysis while preserving user annotations |

Useful trace options:

```bash
python lua_flow.py input.lua --trace --html \
  --lua lua \
  --trace-timeout 10 \
  --max-events 20000 \
  --script-arg value
```

## Live debugger

The Debug Trace workspace combines source, current instruction, breakpoint
gutter, current-frame variables, call stack, console output, and event history.

| Key | Action |
|---|---|
| `F2` | Restart the target and restore line breakpoints |
| `F4` | Run to the selected source line |
| `F5` | Continue until a breakpoint, input request, or exit |
| `F7` | Step into |
| `F8` | Step over |
| `F9` | Toggle a line breakpoint |
| `Shift+F11` | Step out |
| `↑` / `↓` | Move through recorded events |

When the target calls `io.read()`, the debugger changes to **Waiting for input**.
Enter text in the program-input field and press Enter or **Send Input**. This
input is delivered to the target rather than interpreted as a debugger command.

While paused, **Current Locals** and **Current Upvalues** are pinned to the real
active frame. Click **Edit** and enter a restricted-environment Lua expression:

```lua
42
"patched"
false
nil
{ status = "ok", count = 3 }
```

Live mode keeps private references to the original debug functions. Ordinary
Lua-level `debug.gethook()` checks see no controller hook, and target calls to
`debug.sethook()` cannot remove it. This is compatibility-oriented concealment,
not complete protection against native modules, timing checks, OS inspection,
or separately loaded debug libraries.

### Terminal commands

The terminal frontend accepts:

```text
s / step       n / over       o / out       c / continue
break N        clear N        locals        q / quit
```

## Workbench views

| View | Purpose |
|---|---|
| **Source** | Original source order with anchors and control-flow annotations |
| **Execution Flow** | Statements rearranged by deterministic runtime-first `@` order |
| **Graph** | Basic-block CFG with scope selection, drag, pan, zoom, and fit |
| **Analysis** | Searchable function, variable, call, and string XREF index |
| **Patch** | Side-by-side original and browser-only patched draft |
| **Debug Trace** | Recorded replay or live source-level debugger |

Navigation links connect concrete destinations across views:

| Anchor | Meaning |
|---|---|
| `@0x...` | Logical statement |
| `$0x...` | Callable symbol definition or call site |
| `A0x...` | Call-evaluation step |
| `C0x...` | Condition |
| `E0x...` | Short-circuit evaluation step |
| `L0x...` | Label or `goto` destination |

Click a graph node or anchor to jump to source. Use **Backspace** or **Alt+Left**
to return to the previous location.

## Patch workflow

The Patch view never modifies the input file:

```text
Original source ──► browser draft ──► Save .patched.lua
```

- Drafts persist in browser local storage.
- Same-line edits are previewed in Source, Execution Flow, Graph, and Debug.
- `Ctrl+S` or **Save .patched.lua** downloads the completed draft.
- Adding or removing lines invalidates existing source/anchor mappings; download
  the draft and run luaRE again to rebuild exact CFG and XREF data.
- A patch draft does not alter an already running Lua process. Runtime local and
  upvalue editing is a separate debugger feature.

## Projects and outputs

`--project` writes `input.luare.json`. Re-running analysis refreshes CFG and XREF
data while preserving display renames, symbol comments, statement comments,
bookmarks, and graph-layout fields. Projects can also be imported or exported
from the Analysis view.

| File | Contents |
|---|---|
| `input.flow.lua` | Navigation-friendly annotated Lua |
| `input.flow.html` | Standalone interactive workbench or saved trace replay |
| `input.trace.json` | Runtime call, return, line, stack, and value snapshots |
| `input.luare.json` | Persistent analysis database and user annotations |
| `input.patched.lua` | Optional browser patch export |

## What the analyzer understands

- `if`, `elseif`, `else`, loops, `repeat` / `until`, `break`, and `return`
- Function declarations and assigned functions such as `local f = function()`
- Function-local `goto` and deterministic `::label::` destinations
- Semicolon-separated and compact one-line statements
- Top-level `and` / `or` short-circuit condition steps
- Nested calls, duplicate call sites, and per-call evaluation anchors
- Unspecified sibling argument order without inventing false sequencing
- Runtime-first numbering across known function calls
- Straight-line CFG coalescing into basic blocks
- Source-line provenance and stable structure-derived IDs

## Crackme sample

[`example.lua`](./example.lua) is an intentionally layered input-driven sample.
Its expected serial is not stored as plaintext; validation uses an indirect
dispatcher, byte permutation, rolling state, arithmetic guards, and decoy
handlers. It is useful for exercising graph recovery, stepping, input handling,
breakpoints, and runtime value editing.

```bash
python lua_flow.py example.lua --gui
```

## Current limitations

luaRE currently uses a lightweight structural tokenizer rather than a complete
Lua grammar and IR. It is useful for ordinary source and first-pass reverse
engineering, but pathological generated syntax and deeply nested expression
semantics should eventually move to a real AST frontend.

The live debugger is source-line based through `debug.sethook`:

- Multiple logical statements on one physical line can share a runtime event.
- C functions do not expose internal Lua line events.
- Raw Lua VM registers are not available; locals and upvalues are the current
  source-level equivalent.
- Native anti-debugging and full LuaJIT behavior require a custom VM backend.

The codebase keeps the annotation and viewer formats separate from the frontend
so a future parser, bytecode frontend, or custom Lua VM can replace the current
tokenizer without redesigning the workbench.

## Development

Run the test suite:

```bash
python -m unittest
```
