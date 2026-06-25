# AST And IR Direction

KLEVA is moving away from source-text regex shaping toward a typed internal
representation.

The target pipeline is:

```text
C source
  -> compiler AST
  -> KLEVA IR facts
  -> generic shapers
  -> KLEE/EVA candidates
  -> proven tests plus unproved diagnostics
```

The first backend uses:

```text
clang -Xclang -ast-dump=json -fsyntax-only
```

KLEVA then translates only the facts it needs into `kleva.ir` objects. The IR is
small on purpose. It is not trying to replace Clang as a C parser.

Current IR facts include:

- variable references
- integer literals
- unary and binary operators
- field accesses
- calls
- `if` conditions
- `switch` selectors and cases

The first IR-based shaper is `state_switch_candidates_from_ir`. It recognizes a
generic state-machine shape:

```c
switch (obj->state) {
    case STATE_A:
    case STATE_B:
}
```

and emits candidates that assign the switch selector field:

```c
obj->state = STATE_A;
obj->state = STATE_B;
```

This is intentionally domain-independent. The shaper does not know about TCP,
hosts, packets, sockets, or any specific project.

## What This Replaces

Older KLEVA shapers often read raw source text and used regex to infer:

- conditions
- state switches
- table loops
- ownership
- callbacks

That approach grows linearly with special cases and becomes fragile when C
syntax changes shape. The AST/IR direction moves those decisions onto typed
nodes such as `SwitchStmt`, `FieldAccess`, and `BinaryOp`.

## Next Steps

The next useful migrations are:

1. Use IR switch facts inside normal synthesis, beside the existing text shaper.
2. Add IR extraction for assignments, returns, and call sites.
3. Port ownership/free detection to IR call facts.
4. Port branch condition shaping to IR `BinaryOp` and `UnaryOp`.
5. Build state-transition facts from assignments to the same state field.

Regex should become fallback behavior, not the primary source of truth.
