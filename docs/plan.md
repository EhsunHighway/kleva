# KLEVA AST/IR Migration Plan

This plan tracks the migration from regex-heavy source shaping toward a typed
AST/IR-driven architecture.

Status legend:

- `[x]` done and tested
- `[~]` partially done
- `[ ]` not started

## Goal

KLEVA should eventually generate candidates from typed C facts, not from raw
source-text regex.

Target flow:

```text
C source
  -> compiler AST
  -> KLEVA typed IR
  -> generic shapers
  -> KLEE/EVA candidates
  -> proven tests plus unproved diagnostic tests
```

Regex may remain as a temporary fallback, but it should not be the primary
source of truth for branch, state-machine, ownership, callback, or table
reasoning.

## Phase 1: AST/IR Foundation

Status: `[x]` done

Done:

- `[x]` Added `src/kleva/ir/model.py`.
- `[x]` Added typed expression facts:
  - `VarRef`
  - `IntLiteral`
  - `UnaryOp`
  - `BinaryOp`
  - `FieldAccess`
  - `CallExpr`
- `[x]` Added typed statement facts:
  - `IfStmt`
  - `ExprStmt`
  - `AssignmentStmt`
  - `ArraySubscript`
  - `ReturnStmt`
  - `LoopStmt`
  - `SwitchStmt`
  - `SwitchCase`
  - `FunctionIR`
- `[x]` Added Clang JSON AST backend in `src/kleva/ir/clang_json.py`.
- `[x]` Added test proving real Clang AST extraction works.
- `[x]` Added architecture note in `docs/ast-ir.md`.

Remaining:

- `[x]` Add IR node types for assignments.
- `[x]` Add IR node types for array subscripts.
- `[x]` Add IR node types for returns.
- `[x]` Add IR node types for declarations.
- `[x]` Add IR node types for address-of and dereference.
- `[x]` Add IR node types for explicit casts.
- `[x]` Preserve source locations for diagnostics.
- `[x]` Preserve enough type information for object graph shaping.
  - `[x]` Preserve expression/result `qualType` where Clang exposes it.
  - `[x]` Preserve call result and argument expression types.
  - `[x]` Preserve field, array-subscript, cast, return, and assignment
    expression types.
  - `[x]` Use preserved expression types in object graph shaping.

## Phase 2: Wire IR Into Synthesis

Status: `[x]` done

Done:

- `[x]` Parse source into `FunctionIR` inside `generate_yaml_from_header`
  with the Clang JSON backend.
- `[x]` Pass per-function `FunctionIR` into source candidate generation.
- `[x]` Keep regex shapers as fallback when IR extraction is unavailable.
- `[x]` Added synthesis-level regression coverage proving no-YAML synthesis can
  emit IR-backed switch candidates.
- `[x]` Add CLI option:

```sh
--ir-backend clang-json|off
```

Remaining:

- `[x]` Prefer IR facts for more shapers than state switches.
- `[x]` Report IR extraction failures in a controlled debug mode.
- `[x]` Add debug output option:

```sh
--emit-ir path.json
```

```sh
--ir-diagnostics path.json
```

## Phase 3: Replace Switch/State Regex

Status: `[x]` done

Done:

- `[x]` Added first IR-based state-switch shaper:
  `state_switch_candidates_from_ir`.
- `[x]` Added test for generic `switch (obj->state)` candidate generation.

Remaining:

- `[x]` Wire `state_switch_candidates_from_ir` into normal synthesis.
- `[x]` Support enum case names, not only integer case values.
- `[x]` Support nested selector fields such as `ctx->conn->state`.
- `[x]` Support selectors reached through local aliases.
- `[x]` Generate default-case candidates when possible.
- `[x]` Detect state transitions from assignments to the same state field.
- `[x]` Generate IR case-guard candidates by combining a switch case fact with
  typed `if` condition setup from that case body.
- `[x]` Resolve selector and guard aliases for switch case-guard candidates.
- `[x]` Rewrite helper-returned array-slot aliases in switch candidates:
  `result->field` can become `arg->array[0].field` when the helper returns an
  address of an array element.
- `[x]` Add IR fallback-lookup shaping for generic exact-miss then fallback-hit
  flows where helper IR returns an array element address.
- `[x]` Materialize decoded local RHS values in IR lookup setups by writing the
  backing encoded field instead of referencing function-local decoded variables.
- `[x]` Normalize numeric object-like macros during semantic fact suppression,
  so IR facts rendered with literal values can suppress regex facts rendered
  with source macro names.
- `[x]` Treat regex fallback-lookup shaping as fallback-only: when typed IR
  lookup shaping emits candidates for a function, suppress parallel regex
  lookup candidates for that function.

## Phase 4: Replace Branch Condition Regex

Status: `[x]` done

Done:

- `[x]` Added IR condition shaper in `src/kleva/shaping/ir_conditions.py`.
- `[x]` Wired IR condition candidates into normal branch candidate generation.
- `[x]` Shape `BinaryOp` conditions:
  - `==`
  - `!=`
  - `<`
  - `<=`
  - `>`
  - `>=`
- `[x]` Shape `UnaryOp` conditions:
  - `!ptr`
- `[x]` Split `A || B` into separate candidate paths.
- `[x]` Split `A && B` into required setup constraints.
- `[x]` Generate boundary values:
  - below
  - equal
  - above
- `[x]` Added unit coverage for IR comparison, unary, OR, and AND shaping.
- `[x]` Walk nested IR control-flow bodies when searching for condition
  branches.

- `[x]` Use type facts to distinguish scalar variables from pointer objects.
- `[x]` Support nested expressions and casts in condition operands.
- `[x]` Generate explicit false-branch candidates.
- `[x]` Shape generic bitwise flag guards from IR:
  - `flags & MASK`
  - `!(flags & MASK)`
- `[x]` Shape flipped comparison operands from IR:
  - `CONST == field`
  - `CONST < field`
- `[x]` Resolve simple local aliases before shaping IR conditions:
  - `local = obj->field`
  - `if (local == VALUE)`
- `[x]` Move typed alias recording and resolution into shared IR utility code
  used by both branch-condition and switch shapers.
- `[x]` Move shared IR expression rendering into `kleva.ir.render` and use it
  from branch-condition and switch shapers.
- `[x]` Use shared IR expression rendering from callee, table, and parser
  shapers instead of private `_expr_text` / `_assignable_expr` helpers.
- `[x]` Add shared `walk_if_statements` traversal and use it from callee and
  parser shapers instead of private recursive walkers.
- `[x]` Move shared integer-literal and relation helpers into
  `kleva.ir.relations` and use them from condition, parser, and callee
  shapers.
- `[x]` Move shared candidate-name sanitizing into `kleva.ir.naming` and use it
  from switch, table, and callee shapers.
- `[x]` Move direct return-body detection into `kleva.ir.walk` and use it from
  callee and parser shapers.
- `[x]` Add IR decoded-field alias extraction for byte-order conversions such
  as `local = ntoh*(obj->field)` and copied locals.
- `[x]` Use IR decoded-field aliases to generate byte-order branch candidates
  such as `obj->field = hton*(VALUE)` from decoded-local comparisons.
- `[x]` Resolve cast aliases in IR decoded-field targets so IR byte-order
  setups can suppress equivalent regex byte-order fallback candidates.
- `[x]` Prefer IR cast-alias switch candidates before regex casted-field switch
  fallback candidates when their setup is equivalent.
- `[x]` Track typed branch facts on IR candidates so equivalent regex fallback
  candidates are suppressed by branch meaning, even when regex setup includes
  extra harness plumbing.
- `[x]` Attach typed branch facts to plain pointer-field regex fallback
  candidates so IR condition candidates suppress equivalent guarded fallback
  candidates by meaning.
- `[x]` Add true-side typed facts for plain pointer-field `!=` fallback
  candidates so IR can suppress both equality and inequality guarded fallback
  paths.
- `[x]` Use semantic negated relations for byte-order regex fallback branch
  facts so IR false-side candidates suppress equivalent fallback paths by
  meaning.
- `[x]` Treat regex byte-order condition shaping as fallback-only: when typed IR
  condition shaping emits candidates for a function, suppress parallel regex
  byte-order candidates from the source-text path.
- `[x]` Stop broad source-text branch shaping from appending helper function
  bodies to the caller body. Helper behavior is now modeled through helper IR
  and dedicated helper-body analysis, which prevents helper-local branches from
  leaking into caller candidates.
- `[x]` Use typed IR to decide no-ACSL nullable pointer parameters when a
  function has a parameter-specific NULL guard whose body returns. Source-text
  null-guard regex remains only as fallback when IR is unavailable.
- `[x]` Thread IR nullable-parameter facts through the body-generation
  ownership summary, so constructor-guard suppression uses typed IR when
  available and only falls back to source-text null-guard regex without IR.
- `[x]` Use IR ownership summaries as the source of truth for consumed and
  transferred pointer parameters in body generation. Source-text free/transfer
  regex checks now run only when no IR ownership summary is available.
- `[x]` Use IR ownership summaries as the source of truth for owned pointer
  returns in body generation. Constructor/name-based return ownership
  heuristics now run only when no IR ownership summary is available.
- `[x]` Attach typed branch facts to regex state-switch candidates and apply
  IR-preferred suppression to regex state-switch delegates.
- `[x]` Attach typed branch facts to IR and regex table candidates, run IR table
  shaping before regex loop-table fallback, and suppress equivalent regex table
  delegates by meaning.
- `[x]` Attach typed branch facts to fallback-lookup regex candidates and apply
  IR-preferred suppression to fallback-lookup delegates.
- `[x]` Add typed callee-outcome facts to IR and regex callee-success
  candidates and suppress equivalent regex callee-success delegates by call
  outcome.
- `[x]` Add typed branch and call-outcome facts to IR parser candidates so
  numeric/equality boundaries and guarded helper calls are machine-readable.
- `[x]` Add typed callback presence facts to IR callback candidates for direct
  function-pointer parameters and function-pointer fields.
- `[x]` Add typed ownership facts for consumed and transferred parameters, and
  derive ownership summaries from those facts.
- `[x]` Add a unified `BranchCandidate.semantic_facts()` API and make
  IR-preferred suppression consume semantic facts generically instead of
  knowing each fact field separately.
- `[x]` Serialize candidate semantic facts into synthesized YAML so candidate
  intent is inspectable without reading setup statements.
- `[x]` Preserve and render candidate semantic facts in report-only coverage
  summaries.
- `[x]` Preserve candidate semantic facts through YAML parsing, recipe
  construction, unproved diagnostic tests, and unproved candidate reports.
- `[x]` Keep regex branch shapers as compatibility fallback behind
  `regex-fallbacks`, with typed semantic facts used for IR-preferred
  suppression.

## Phase 5: Replace Ownership Regex

Status: `[x]` done

Done:

- `[x]` Extract direct call statements from Clang JSON AST into `ExprStmt`.
- `[x]` Detect direct free/consume calls from IR `CallExpr`.
- `[x]` Added generic ownership shaper:
  `src/kleva/shaping/ir_ownership.py`.
- `[x]` Added tests for direct `free`, destructor-style names, explicit
  consuming callees, and non-parameter arguments.
- `[x]` Detect ownership transfer from IR assignments:

```c
owner->field = ptr;
```
- `[x]` Added tests for parameter-to-owner-field transfer and non-parameter
  assignment filtering.
- `[x]` Detect ownership transfer from array/subscript assignments:

```c
array[count++] = ptr;
```
- `[x]` Added parser and ownership-shaper tests for array/subscript transfer.
- `[x]` Classify pointer behavior:
  - borrowed
  - consumed/freed
  - stored/transferred
  - returned owned pointer
- `[x]` Walk nested IR control-flow bodies for ownership facts:
  - consumed parameters
  - transferred parameters
  - returned owned pointers
- `[x]` Use this classification for heap-vs-stack fixture choice.
- `[x]` Use this classification for cleanup generation.
- `[x]` Keep source-text ownership checks only as fallback when IR is
  unavailable.

## Phase 6: Object Graph Builder

Status: `[x]` done

Done:

- `[x]` Existing fixture construction can create complete stack structs.
- `[x]` Existing fixture construction handles pointer-to-pointer fields with a
  slot variable.
- `[x]` Existing fixture construction can recursively back pointer fields to
  complete structs.
- `[x]` `CParam` tracks pointer depth explicitly.
- `[x]` Fixture construction uses `CParam.pointer_depth` instead of raw string
  counting.
- `[x]` Complete struct setup supports arrays of complete structs.
- `[x]` Complete struct setup supports arrays of pointers to complete structs.

Remaining:

- `[x]` Move object graph construction to typed AST/IR type facts.
  - `[x]` Extract typed object-path facts from IR branch conditions.
  - `[x]` Extract typed object-path facts from IR switch selectors.
  - `[x]` Use typed object-path facts to back nested complete-struct pointer
    fields before candidate setup lines assign through them.
- `[x]` Track pointer depth explicitly in the type model.
- `[x]` Support arrays of structs and arrays of pointers.
- `[x]` Support function pointer fields from IR/type facts.
- `[x]` Generate object graphs from required path facts, not from broad
  recursive defaults.

## Phase 7: Table/Loop Shaper

Status: `[x]` done

Done:

- `[x]` Extract loop facts from AST/IR.
- `[x]` Preserve loop body statements needed by shapers.
- `[x]` Detect generic lookup loops from array-field equality conditions.
- `[x]` Generate table hit candidates.
- `[x]` Generate table miss candidates.
- `[x]` Wire IR table candidates into normal branch candidate generation.
- `[x]` Generate full-table candidates.
- `[x]` Generate first-free-slot candidates.
- `[x]` Generate duplicate-key candidates.
- `[x]` Walk nested IR control-flow bodies when searching for lookup loops.
- `[x]` Replace table regex shaper in the main path with IR table shaper while
  keeping the existing source-text shaper as fallback.

## Phase 8: Callback Shaper

Status: `[x]` done

Done:

- `[x]` Detect function pointer field calls from IR `CallExpr`.
- `[x]` Detect callback field expressions.
- `[x]` Generate callback-null candidates.
- `[x]` Generate callback-present candidates with typed stubs.
- `[x]` Wire IR callback candidates into normal branch candidate generation.

Remaining:

- `[x]` Detect direct function-pointer parameter calls from IR and type facts.
- `[x]` Emit callback witness outputs.
- `[x]` Replace remaining callback/source-text shaping with IR callback facts.
- `[x]` Detect callback guards such as `if (ctx->handler)` from IR `IfStmt`
    conditions.
  - `[x]` Walk nested IR control-flow bodies for callback calls and guards.
  - `[x]` Remove the old source-text callback guard fallback after IR switch
    bodies cover the same nested locations.

## Phase 9: Callee Success/Failure Shaper

Status: `[x]` done

Done:

- `[x]` Detect callee return guards from IR:

```c
if (callee(...) != 0) return -1;
if (!callee(...)) return -1;
if (callee(...) == -1) return -1;
if (callee(...) < 0) return -1;
```

- `[x]` Generate callee-failure candidates.
- `[x]` Generate callee-success candidates.
- `[x]` Walk nested `if` guards inside loop bodies.
- `[x]` Wire IR callee candidates into normal branch candidate generation.
- `[x]` Mark callee-success candidates for witness output.
- `[x]` Preserve call argument expressions on IR callee guards.
- `[x]` Normalize unary negative integer literals such as Clang's `-1`.
- `[x]` Use IR callee arguments with the generic callee-body guard inverter to
  shape simple successful-callee dependency state.
- `[x]` Shape scalar callee dependencies such as `if (size == 0) return -1`
  by creating mutable scalar fixtures when candidates assign scalar params.

Remaining:

- `[x]` Extend dependency-state shaping beyond simple callee precondition
  inversion.
- `[x]` Emit side-effect witnesses after successful callees.

## Phase 10: Parser/Header Shaper

Status: `[x]` done

Done:

- `[x]` Added IR parser/boundary shaper in
  `src/kleva/shaping/ir_parsers.py`.
- `[x]` Detect numeric early-return guards without using domain names:

```c
if (n < 8) return -1;
if (owner->available < 20) return -1;
```

- `[x]` Generate below, equal, and above boundary candidates.
- `[x]` Handle flipped comparisons such as `20 > owner->available`.
- `[x]` Detect equality early-return guards without using domain names:

```c
if (tag != 7) return -1;
if (3 == owner->kind) return -1;
```

- `[x]` Generate matching and nonmatching candidates for equality guards.
- `[x]` Wire parser boundary candidates into normal branch candidate
  generation behind `parser-headers`.
- `[x]` Enable `parser-headers` in default shaping.
- `[x]` Added no-YAML synthesis coverage proving real Clang IR emits boundary
  candidates.
- `[x]` Reuse IR switch default candidates as unsupported/default type
  candidates for switch-based dispatch.
- `[x]` Detect call-in-condition helper guards without using helper names:

```c
if (verify(buf, len) != 0) return -1;
```

- `[x]` Generate named helper-call success and failure candidates for
  diagnostic/proof exploration.
- `[x]` Added an internal generic helper-call repair rule model:

```text
callee + success_setup templates + failure_setup templates
```

- `[x]` Helper repair templates can reference call arguments with placeholders
  such as `{arg0}`, without hardcoding helper names in KLEVA.
- `[x]` Wire helper-call repair rules through the internal no-YAML synthesis
  API.
- `[x]` Load helper-call repair rules from YAML files.
- `[x]` Expose helper-call repair rules through `kleva synth --helper-rules`
  and `kleva run --helper-rules`.
- `[x]` Document helper-call repair rule files and add a public example.

Remaining:

- `[x]` Refine parser/header naming beyond generic numeric guards.
- `[x]` Detect simple magic/type/version-style constant guards.
- `[x]` Detect simple unsupported/default type branches from `switch default`.
- `[x]` Detect checksum-style helper calls as generic call-in-condition guards.
- `[x]` Generate too-short, exact-minimum, and valid-length candidates for
  simple numeric minimum guards.
- `[x]` Generate supported and unsupported candidates for simple equality
  guards.
- `[x]` Generate bad-checksum and good-checksum candidates when an explicit
  helper-call repair rule is available.
- `[x]` Add executable helper models for cases where no explicit repair rule is
  provided.

## Phase 11: Diagnostic Unproved Integration

Status: `[x]` done

Done:

- `[x]` Added `--emit-unproved {off,report,tests,all}`.
- `[x]` Proven tests remain separate from diagnostic tests.
- `[x]` Unproved candidate tests are emitted separately when requested.
- `[x]` Unproved outputs omit hard assertions and are marked
  `EVA_UNPROVED`.
- `[x]` Optional report file can be emitted.
- `[x]` Added unit coverage for separate unproved candidate generation.
- `[x]` Add initial reason classification for unproved candidate diagnostics:
  - fixture gap
  - missing contract or observable
  - EVA imprecision
  - possible implementation bug
- `[x]` Include reason categories in diagnostic C comments and unproved reports.
- `[x]` Include KLEE artifact status in diagnostic C comments and unproved
  reports:
  - `ktest_available` when a concrete `.ktest` path is attached to the recipe
  - `not_recorded` when the recipe was built without a tracked KLEE artifact
- `[x]` Preserve source-origin and target-branch metadata from typed branch
  candidates through YAML/config, recipes, diagnostic C comments, and
  unproved reports.
- `[x]` Emit honest internal IR source locations such as `ir:run:switch[0]`
  until physical source line preservation exists.

## Phase 12: Coverage Reporting

Status: `[x]` done

Tasks:

- `[x]` Add report-only coverage summary structures.
- `[x]` Map generated candidates to covered branches when source/branch
  metadata matches.
- `[x]` Report uncovered branches with no candidate.
- `[x]` Report unproved candidates separately from uncovered branches.
- `[x]` Do not use gcov/gcovr as a generation driver in the coverage summary
  layer.
- `[x]` Add an optional CLI/report entry point that consumes external coverage
  facts without feeding them back into synthesis.
- `[x]` Document the external coverage-fact YAML shape and
  `kleva coverage-report` command.

## Phase 13: Deprecate Regex Shapers

Status: `[x]` done

Tasks:

- `[x]` Add equivalence tests where IR and regex produce the same simple
  candidates.
- `[x]` Prefer IR shapers when an IR-backed candidate and a regex candidate
  produce the same setup.
- `[x]` Move regex shapers behind the `regex-fallbacks` feature flag.
- `[x]` Disable `regex-fallbacks` by default after the AST/IR shapers became
  the normal synthesis path.
- `[x]` Preserve candidate origin metadata (`ir`, `regex`, or
  `not_recorded`) through generated YAML, recipes, diagnostics, and
  report-only coverage summaries.
- `[x]` Add a conservative regex-retirement gate to the report-only coverage
  summary. Regex paths are removable only when generated candidate evidence is
  IR-origin, proven, covered, and has no unknown-origin or uncovered-branch
  blockers.
- `[x]` Isolate ACSL parsing behind `AcslParser` / `ScannerAcslParser`.
- `[x]` Make ACSL parsing an injectable synthesis dependency, so the
  contract parser is replaceable rather than a hidden source-query dependency.

## Phase 14: Physical Source Locations

Status: `[x]` done

Tasks:

- `[x]` Add `SourceLocation` to the typed IR model.
- `[x]` Extract statement locations from Clang JSON `range.begin`.
- `[x]` Preserve physical source locations on `if` and `switch` statements.
- `[x]` Prefer physical locations in IR condition and switch candidates.
- `[x]` Preserve physical locations for loop/table/callback/callee/parser
  candidates where the backing IR statement is available.
- `[x]` Surface physical source locations in coverage examples and docs.

## Current Verified State

- `[x]` KLEVA has a Clang-backed IR foundation.
- `[x]` KLEVA has IR-based shapers for branch conditions, switches, tables,
  callbacks, callees, parser/header guards, and ownership facts.
- `[x]` KLEVA uses IR shapers in the main synthesis path. Regex shapers are
  compatibility fallback behind the opt-in `regex-fallbacks` feature.
- `[x]` KLEVA exposes `--ir-backend clang-json|off`.
- `[x]` KLEVA exposes `--emit-ir path.json` for typed IR inspection.
- `[x]` KLEVA can emit unproved diagnostic candidates separately.
- `[x]` Candidate semantic facts are preserved through YAML, config, recipes,
  unproved diagnostics, and coverage reports.
- `[x]` No-YAML `mode gen` reports when synthesized candidates have no KLEE
  recipes yet, instead of leaving zero-recipe candidates unexplained.
- `[x]` IR condition shaping no longer emits harness assignments to
  implementation-local variables that are not reachable from the test.
- `[x]` Pointer fixture construction prefers visible constructors for complete
  struct pointer parameters, so generated tests use module invariants when
  constructor APIs exist.
- `[x]` EVA probes use `Frama_C_assume` for required fixture guards while KLEE
  harnesses keep runtime early-return guards and unit tests keep assertions.
- `[x]` Generated probe/unit/KLEE function bodies drop local typedef lines, so
  Frama-C does not fail on block-level typedef redeclarations already provided
  by the included module header.
- `[x]` IR condition shaping preserves related object invariants for
  field-to-field comparisons, so candidates such as `count >= capacity` shape
  small correlated values instead of only mutating one field.
- `[x]` IR condition and parser/header candidates carry continuation
  object-path facts from later statements, so fixture construction can back
  pointer-array slots before candidates execute paths that dereference them.
- `[x]` Successful callee candidates now record typed post-state facts for
  side-effect witness targets, so reports and YAML carry callee side-effect
  meaning without reinterpreting setup strings.
- `[x]` IR callee-success shaping can infer simple post-state facts directly
  from visible helper IR assignments and map helper parameters back to caller
  arguments.
- `[x]` Helper IR post-state inference resolves local aliases when collecting
  simple assignments.
- `[x]` Helper IR post-state inference maps simple out-parameter writes such
  as `*out = 1` back to caller arguments, including address arguments like
  `&slot`.
- `[x]` IR callee-success shaping detects helper return values stored in local
  variables and later checked by early-return guards, such as
  `item = lookup(table); if (!item) return -1;`.
- `[x]` Helper IR post-state inference propagates simple facts from returned
  parameter objects to caller-side result aliases when helpers return a
  parameter or local alias of a parameter.
- `[x]` Helper IR post-state inference is conservative about path sensitivity:
  straight-line assignments before a return are treated as guaranteed, while
  assignments hidden behind unknown conditionals are not emitted as witness
  post-state facts.
- `[x]` Helper IR post-state inference now performs simple branch-aware
  intersection for `if` bodies: a fact is emitted only when every modeled
  terminal path establishes it.
- `[x]` Helper IR post-state inference distinguishes success returns from
  failure returns for common guarded-callee modes such as nonzero failure,
  zero failure, negative failure, nonpositive failure, and exact-value failure.
- `[x]` Helper IR post-state inference handles modeled switch bodies
  conservatively: switch bodies contribute facts, and switches without default
  keep a selector-miss path.
- `[x]` Clang-backed IR preserves per-case and default switch bodies, and
  helper-call dataflow intersects post-state facts across those case/default
  paths.
- `[x]` Verified no-YAML `event.c` run from scratch:
  19 test vectors, 30 EVA-proven assertions, 0 unproved candidate paths,
  0 skipped candidates.
- `[x]` Validated typed branch/state/lookup shaping against a no-YAML TCP
  generation run: generated YAML had 296 candidate entries, 0 regex
  `source_*` branch candidates, and retained typed IR fallback-lookup
  candidates for listener lookup paths.
- `[x]` Validated typed condition/callee shaping against a no-YAML `host.c`
  generation run: generated YAML had 56 candidate entries and 0 regex
  `source_*` branch candidates.
- `[x]` Validated typed condition/table shaping against a no-YAML
  `arp_cache.c` generation run: generated YAML had 102 candidate entries,
  30 IR table candidates, and 0 regex `source_*` branch candidates.
- `[x]` Validated typed condition shaping against a no-YAML `arp.c`
  generation run: generated YAML had 15 candidate entries, 0 regex
  `source_*` branch candidates, and successful clang-json IR diagnostics.
- `[x]` Revalidated no-YAML TCP and ARP cache generation after removing
  helper-body source expansion. Candidate counts stayed stable:
  TCP had 296 candidates with 0 regex `source_*`; ARP cache had 102
  candidates, 30 IR table candidates, and 0 regex `source_*`.
- `[x]` No-ACSL null candidate discovery is IR-backed when clang-json IR is
  available. A synthesis integration test now proves `if (!param) return ...`
  generates a null behavior without source-text null-guard parsing.
- `[x]` Body generation uses IR nullable-parameter facts for consumed pointer
  setup decisions. A regression test verifies regex null-guard detection is
  not consulted when an IR ownership summary is present.
- `[x]` Body generation skips source-text ownership probes when IR ownership
  facts are present. Regression tests verify consumed/transferred pointer setup
  does not call regex free/transfer detection in the IR-backed path.
- `[x]` Body generation skips source-text returned-ownership heuristics when
  IR return ownership facts are present. A regression test verifies cleanup is
  added from IR ownership without calling the naming heuristic.
- `[x]` Body generation uses IR buffer-use facts for `len`/`data` object
  fixture shaping when IR is available. The source-text buffer detector remains
  only as a no-IR compatibility fallback.
- `[x]` Body generation uses IR void-pointer cast facts for `void *`
  parameter fixture shaping when IR is available. Source-text cast scanning
  remains only as a no-IR compatibility fallback.
- `[x]` Free/destroy cleanup lookup prefers parsed function declarations.
  Source-text visibility scanning remains only as a declaration-missing
  compatibility fallback.
- `[x]` `regex-fallbacks` is no longer enabled by default. The normal
  no-YAML path now uses AST/IR shapers by default, and older text/regex
  shapers are opt-in compatibility via `--shaping all` or
  `--shaping regex-fallbacks`.
- `[x]` Branch candidate generation no longer requires source-body extraction
  before running IR shapers. Source-body alias parsing is skipped unless
  `regex-fallbacks` is enabled.
- `[x]` IR callee-success shaping now inverts simple helper-IR failure guards
  to build success preconditions. The default path no longer depends on
  source-text helper-precondition inversion.
- `[x]` Function declaration maps are now available from Clang JSON AST.
  Synthesis prefers Clang-derived helper/function signatures when
  `--ir-backend clang-json` is active.
- `[x]` Added a no-YAML regression proving helper signatures still work when
  the old text declaration parser is patched out.
- `[x]` Type catalogs are now available from Clang JSON AST. Synthesis prefers
  Clang-derived complete/opaque structs, fields, and function-pointer typedefs
  when `--ir-backend clang-json` is active.
- `[x]` Added a no-YAML regression proving complete struct and function-pointer
  field shaping still work when the old text type parser is patched out.
- `[x]` Source-text function/type metadata is now lazy fallback only. The
  normal clang-json path does not call the old text declaration or type
  parsers unless IR extraction is disabled or fails.
- `[x]` Added no-YAML regressions that fail if the normal clang-json synthesis
  path secretly calls the old text declaration/type metadata parsers.
- `[x]` Public header function discovery is now Clang AST-backed in the
  normal clang-json synthesis path. The old header regex parser remains only
  as fallback when clang-json is disabled or header AST extraction fails.
- `[x]` Header AST discovery filters declarations to the target header, so
  functions from included headers are not mistaken for the module API.
- `[x]` Added no-YAML regressions proving normal synthesis still works when
  the old header regex parser is patched out.
- `[x]` Raw source text is now passed into body/branch generation only for
  explicit fallback use: when clang-json extraction is disabled/failed or
  `regex-fallbacks` is enabled. The normal IR path passes `None` to
  fallback-only source scanners.
- `[x]` Added a no-YAML regression that fails if the normal clang-json path
  calls the old source-body scanner while generating IR-backed branch
  candidates.
- `[x]` Revalidated default TCP synthesis after disabling regex fallback:
  272 IR-origin candidates, 0 regex-origin candidates, and no
  `regex-fallbacks` feature in the synthesized shaping list.
- `[x]` Revalidated representative no-YAML synthesis after making source-text
  metadata fallback lazy:
  event 10/10 IR candidates, scheduler 23/23, ARP cache 90/90, ARP 9/9,
  host 42/42, TCP 272/272, all with 0 regex-origin candidates and no
  `regex-fallbacks` feature.
- `[x]` KLEVA's default C candidate-generation path is AST/IR-driven.
- `[x]` Replaced the regex-backed ACSL parser with `ScannerAcslParser`.
  The old `RegexAcslParser` name remains as a compatibility alias.
- `[x]` Moved old source-text fallback entry points behind
  `compat/source_fallbacks.py`, so fallback use is explicit in imports and call
  sites.
- `[x]` Generated plans now report fallback status with `# Fallbacks: none` or
  `# Fallbacks: used`, and fallback reasons are emitted as YAML comments.
- `[x]` `kleva synth` prints fallback warnings when IR is disabled, IR
  extraction fails, or `regex-fallbacks` is requested.
- `[x]` `--ir-diagnostics` now records `source-fallback` entries when fallback
  is used.
- `[x]` Revalidated no-YAML end-to-end `event.c` after fallback isolation:
  19 test vectors, 30 EVA-proven assertions, 0 unproved candidate paths,
  0 skipped candidates.
- `[x]` Revalidated no-YAML end-to-end `scheduler.c` after fallback isolation:
  32 test vectors, 33 EVA-proven assertions, 3 unproved candidate paths,
  and 3 skipped candidates.
- `[x]` Fixed generic IR condition shaping for pointer truthiness. Root
  pointer variables now use a non-null guard instead of assigning scalar `1`
  or an invalid fake object pointer.
- `[x]` Fixed generic local-root filtering for candidate setup lines. Setup
  that references function-local roots in expressions such as
  `table[local->field]` is skipped instead of generating uncompilable
  harness code.
- `[x]` Fixed source witness output generation for array fields. KLEVA no
  longer emits invalid scalar witnesses such as `int out_field = obj.array;`.
- `[x]` Fixed IR field rendering for embedded structs. Field access rendering
  now uses `.` for non-pointer struct expressions and `->` for pointer
  expressions, so generic object paths such as `host->base.interfaces` compile.
- `[x]` Fixed nested source witness generation for array fields. KLEVA now
  skips array witnesses even when the array is inside an embedded struct field,
  avoiding invalid outputs such as `int out_base_name = obj.base.name;`.
- `[x]` Started no-YAML end-to-end `host.c` smoke. KLEE generated 56 recipes
  without YAML and got past the earlier code-generation failures. EVA reached
  the generated proof phase but the broad `host_free_valid` ownership case was
  too expensive for an interactive smoke run before interruption.
- `[x]` Rechecked no-YAML `host.c` with `--mode gen --eva-timeout 5`. The
  pipeline reused the 56 KLEE recipes and correctly timed out expensive EVA
  probes instead of hanging indefinitely. The remaining issue is proof cost and
  candidate prioritization for large modules, not YAML dependence.

## Immediate Next Step

Tighten large-module smoke behavior so expensive ownership/free probes are
reported quickly as unproved or timed out, then rerun no-YAML `host.c` to
completion before moving to `tcp.c`.
