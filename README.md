# luare

A zero-dependency, analysis-oriented Lua source annotator.

It rewrites Lua into a navigation-friendly form where each logical statement has an `@` anchor, control-flow conditions have `cond` / `branch` comments, and callable symbols share `$` anchors between call sites and definitions.

Call annotations also record the callee statement and the continuation point after the call. Terminal statements such as `return` and `break` are annotated with their runtime destination instead of a misleading source-order `next` edge.

## Usage

```bash
python lua_flow.py input.lua --html
```

Outputs:

- `input.flow.lua` — annotated Lua text
- `input.flow.html` — interactive viewer

In the HTML viewer, click `@0x...`, `$0x...`, `A0x...`, `C0x...`, `E0x...`, or `L0x...` links to jump to the concrete destination. Press **Backspace** or **Alt+Left** to return to the previous view.

## Current MVP scope

- semicolon splitting outside strings/brackets
- compact one-line `if ... then ... else ... end` splitting
- `if / elseif / else / end`
- `while / for / repeat / until`
- function-local `goto` / `::label::` resolution with deterministic label IDs
- top-level `and` / `or` short-circuit steps inside conditions
- function definition anchors
- function call references
- nested call evaluation dependencies with per-call `A0x...` anchors
- duplicate call sites preserved as distinct evaluation anchors
- sibling argument call order marked `sibling-order:unspecified` instead of inventing an order
- clickable condition/call-evaluation/label anchors in the HTML viewer
- short-circuit routes resolve directly to deterministic `E0x...` evaluation anchors
- deterministic IDs, so re-running the same structure remains easy to diff
- source line provenance in annotations

## Important limitation

This version is intentionally dependency-free and uses a lightweight structural tokenizer, not a full Lua grammar. It is useful for ordinary source and first-pass reverse engineering, but highly pathological grammar cases, generated code, and full expression-level short-circuit normalization (`a and foo()`, complex table/index expressions, etc.) should eventually move to a real AST/IR frontend.

The project is structured so that frontend replacement does not change the annotation/viewer format.
