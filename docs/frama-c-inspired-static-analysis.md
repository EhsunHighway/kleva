# Frama-C-Inspired Static Analysis Architecture

This document specifies the static-analysis direction for KLEVA.

The goal is not to copy Frama-C. The goal is to learn from the architectural
shape that makes Frama-C usable for many C analyses: a shared kernel, a typed
program representation, formal contracts as first-class input, modular
analyzers, explicit proof statuses, and controlled fallbacks.

KLEVA has a different mission. It generates regression tests by combining:

- C source and headers
- ACSL contracts
- KLEE reachability
- Frama-C EVA proof of generated oracles

The architecture below keeps that mission, but moves KLEVA away from
source-text pattern matching and toward static-analysis facts.

## Frama-C Lessons To Adapt

### Shared Kernel

Frama-C plugins do not each parse C independently. They rely on a common
front-end, normalized program representation, project model, and annotation
model.

KLEVA should follow the same principle:

```text
C source + headers + ACSL
  -> KLEVA front-end
  -> typed program model
  -> analysis facts
  -> candidate generators
  -> KLEE/EVA pipeline
```

No shaper should need to rediscover C syntax from raw text when typed facts are
available.

### Typed Program Representation

KLEVA must reason over typed objects such as:

- functions
- parameters
- declarations
- assignments
- calls
- returns
- conditions
- loops
- switches
- fields
- array accesses
- casts
- address-of and dereference expressions

The internal representation does not need to model the whole C language at
Frama-C depth. It only needs enough typed structure to generate valid fixtures,
path candidates, and post-call observables.

### Contracts As Semantic Input

ACSL is not only an assertion source. It is also a fixture source.

For example:

```c
assumes \valid(pkt);
assumes pkt->len >= 20;
assumes \valid_read(pkt->data + (0 .. pkt->len - 1));
ensures \result == 0 ==> iface->tx_bytes >= \old(iface->tx_bytes);
```

KLEVA should extract facts from these clauses:

- `pkt` must be allocated and non-null.
- `pkt->len` must be at least 20.
- `pkt->data` must point into a readable buffer.
- `iface->tx_bytes` should be snapshotted before the call.
- A success candidate should observe the post-call `tx_bytes` relation.

This is the closest KLEVA equivalent to Frama-C-style context generation from
formal specifications.

### Modular Analyzers

Frama-C's strength comes from analysis modules sharing the same program model.
KLEVA should have the same shape.

Core modules should own:

- parsing
- type information
- function maps
- ACSL contracts
- object graph facts
- CFG/path facts
- candidate and oracle data models

Specialized shapers should be replaceable modules:

- branch shaper
- switch/state-machine shaper
- table/loop shaper
- callback shaper
- ownership shaper
- allocation-failure shaper
- byte-order shaper
- parser/decoder shaper
- side-effect witness shaper
- curated value/content diversity shaper

If a shaper needs project-specific vocabulary, that vocabulary belongs in an
explicit rule/plugin file, not in generic Python code.

### Explicit Unknowns

Frama-C analyses distinguish proved, unknown, invalid, timeout, and alarm-like
states. KLEVA needs the same discipline.

KLEVA test generation should classify every candidate:

- `trusted`: KLEE reached the path and EVA proved the oracle.
- `diagnostic`: KLEE reached the path but EVA did not prove the oracle.
- `unreachable`: KLEE could not reach the candidate path.
- `weak_fixture`: KLEVA could not construct the required input context strongly
  enough.
- `weak_oracle`: the candidate ran, but KLEVA had no meaningful observable to
  assert.
- `missing_acsl`: a contract or shaper-supplied witness is probably missing.
- `eva_imprecision`: EVA reached the candidate but did not prove every
  requested singleton.
- `implementation_bug`: the concrete candidate exposes behavior that does not
  match the requested observable and no fixture/oracle explanation is visible.
- `timeout`: the tool chain did not finish within the configured budget.

Skipping candidates silently is not acceptable.

## Target Architecture

```text
                 +----------------------+
                 | C source + headers   |
                 | ACSL contracts       |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Front-End Kernel     |
                 | - Clang AST import   |
                 | - ACSL parser        |
                 | - type catalog       |
                 | - function map       |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | KLEVA Typed IR       |
                 | - functions          |
                 | - statements         |
                 | - expressions        |
                 | - source locations   |
                 | - type facts         |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Analysis Fact Layer  |
                 | - nullness           |
                 | - scalar ranges      |
                 | - object paths       |
                 | - ownership          |
                 | - buffer shapes      |
                 | - state values       |
                 | - call outcomes      |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Fixture Graph Builder|
                 | - objects            |
                 | - buffers            |
                 | - strings            |
                 | - tables             |
                 | - callbacks          |
                 | - aliases            |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Candidate Generators |
                 | - path candidates    |
                 | - state candidates   |
                 | - callee candidates  |
                 | - failure candidates |
                 | - oracle candidates  |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | KLEE + EVA Pipeline  |
                 | - reachability       |
                 | - concrete recipes   |
                 | - singleton proof    |
                 | - diagnostics        |
                 +----------------------+
```

## Analysis Fact Domains

KLEVA should use small, practical abstract domains. These are not full EVA
domains. They are fixture-generation domains.

### Nullness

Tracks whether an expression is:

- definitely null
- definitely non-null
- unknown

Examples:

- `if (!ctx) return -1;` creates a null candidate and a non-null continuation.
- `assumes \valid(ctx);` creates a non-null fixture requirement.

### Scalar Ranges

Tracks simple relations and boundary values:

- equal
- not equal
- less than
- less or equal
- greater than
- greater or equal
- small curated boundary set

Examples:

- `len < 8`
- `len == capacity`
- `i < TABLE_SIZE`

### Object Paths

Tracks paths through structs, pointers, and arrays:

```text
ctx->state
table->entries[0].valid
pkt->data
pkt->head
```

Object paths are the bridge between typed IR and generated C setup code.

### Buffer Shapes

Tracks memory relationships:

- base pointer
- current pointer
- capacity
- readable range
- writable range
- length field
- offset from base

This domain is needed for packets, arrays, strings, and generic byte buffers.

### Ownership

Tracks whether an object is:

- borrowed
- owned by caller
- consumed by callee
- transferred to another callee
- returned as newly owned

This prevents KLEVA from generating post-call witnesses that read freed or
transferred objects.

### State Values

Tracks enum-like fields used in state machines:

```c
switch (tcb->state) {
    case TCP_ESTABLISHED:
    case TCP_FIN_WAIT_1:
}
```

The state-machine shaper should be generic. It should not know TCP names. It
only needs to recognize that a field controls a switch and that assignments to
the same field are state transitions.

### Call Outcomes

Tracks helper-call effects:

- call returns success
- call returns failure
- call allocates
- call frees
- call mutates an object
- call schedules/registers a callback

This is necessary for candidates that depend on successful callees. For
example, a test that reaches "queue slot became valid" needs KLEVA to build
enough context for the helper call to succeed.

## Fixture Graph Builder Specification

The fixture graph builder is the next central piece of KLEVA.

Its input is a set of typed requirements:

```text
name is non-null readable char pointer
mac is readable 6-byte array
pkt is valid
pkt->data has at least pkt->len readable bytes
ctx->table->entries[0].valid == 1
```

Its output is generated C setup code:

```c
char name_buf[] = "kleva";
const char *name = name_buf;

uint8_t mac[6] = {0};

Packet *pkt = packet_create(64);
pkt->len = 20;
```

The builder must be generic. It should not know about `Packet`,
`Interface`, `TcpTable`, or any networking-specific name unless that knowledge
is supplied through explicit rules or type facts.

### Required Object Kinds

The builder must support:

- scalar values
- pointers to structs
- embedded structs
- arrays
- pointer arrays
- byte buffers
- strings
- function pointers
- opaque pointers
- aliases between local variables and object paths

### Required Failure Modes

When it cannot build a fixture, it must report why:

- unknown type
- missing allocator strategy
- ambiguous ownership
- unsafe pointer relation
- unsupported ACSL expression
- conflicting constraints

It must not silently use `NULL` for a valid pointer requirement.

## Candidate Generation Specification

Candidate generation should happen in layers.

### Contract Candidates

Generated from ACSL behaviors:

- one candidate per behavior when possible
- valid setup from `assumes`
- expected outputs from `ensures`
- assigned-state witnesses from `assigns` and postconditions

### Path Candidates

Generated from typed IR:

- null and non-null guard paths
- scalar boundary paths
- switch cases
- loop/table hit and miss paths
- helper success and failure paths
- callback-present and callback-absent paths

### State-Machine Candidates

Generated from control-state fields:

- one candidate per switch case
- one candidate per transition assignment
- one candidate per guarded transition when the guard is simple enough

The state-machine shaper must work for any enum-like field, not only TCP.

### Side-Effect Candidates

Generated from assignments and known helper effects:

- field changed
- counter incremented
- queue slot occupied
- callback installed
- event scheduled
- ownership transferred

Side-effect candidates should be path-sensitive. A post-state witness from a
success path must not be attached to an early-return failure path.

## Oracle Classification

KLEVA should emit both trusted and diagnostic tests.

Trusted tests are normal regression tests:

```text
KLEE reached the path
EVA proved the oracle singleton
```

Diagnostic tests are review artifacts:

```text
KLEE reached the path
EVA could not prove the oracle
```

Diagnostic tests are useful because an EVA failure can mean:

- `missing_acsl`
- `weak_fixture`
- `weak_oracle`
- `eva_imprecision`
- `timeout`
- `implementation_bug`

KLEVA should keep these categories visible.

### Curated Diversity

Input diversity is useful, but it must not become blind scalar flooding.

KLEVA's generic diversity shaper creates optional candidates that change one
input dimension at a time:

- compact scalar boundary values
- length-like values such as `0`, `1`, and `2`
- byte-buffer patterns such as all zero, all `0xff`, and first byte set

These candidates still go through the same KLEE/EVA promotion rule as every
other candidate. They are not trusted tests until EVA proves their requested
oracles.

Curated diversity must stay bounded:

- Shape one input dimension per candidate.
- Use concrete call-argument overrides for scalar diversity, so KLEE cannot
  re-symbolize a value that the candidate is meant to fix.
- Respect simple ACSL scalar constraints such as `len > 0`, `n != 0`,
  `x <= 8`, and `0 < capacity`; values outside the active contract are not
  normal trusted-path diversity candidates.
- Do not treat unknown typedefs or function-pointer parameters as assignable
  scalar values.

### Trusted And Diagnostic Tests

Generated tests are separated by proof status:

- Trusted tests are emitted only when KLEE reaches the candidate and EVA proves
  the singleton oracle.
- Diagnostic tests are emitted when the candidate is useful evidence but EVA
  does not prove the oracle.
- Post-state witnesses may only reference values visible in the generated
  harness. A callee-local temporary such as `new_events` inside the function
  body is not a valid expected value for a generated test.

Diagnostic tests are intentionally review material. They can point to a weak
fixture, weak oracle, missing ACSL, EVA imprecision, timeout, or implementation
bug, but they must not be mixed into the trusted regression file.

## Compatibility Boundary

Regex and source-text heuristics are allowed only behind a compatibility
boundary.

Rules:

- The default path should prefer typed AST/IR facts.
- If KLEVA falls back to source text, it must report that fallback.
- Project-specific behavior must be expressed through user rules/plugins.
- New generic features should not add networking names to the KLEVA core.

## Migration Plan

### Phase A: Document The Kernel Boundary

Status: `[x]` done

- `[x]` Define the static-analysis architecture.
- `[x]` Define the fixture graph builder role.
- `[x]` Define candidate and oracle statuses.
- `[x]` Define the compatibility/fallback rule.
- `[x]` Add `src/kleva/kernel` with `ProgramInput`, `ProgramModel`, and
  `build_program_model`.
- `[x]` Route YAML/no-YAML synthesis through the kernel for header functions,
  ACSL specs, visible source text, typed IR, function declarations, type
  catalog, and fallback diagnostics.
- `[x]` Move synthesis entry-point lookups for function declarations,
  function IR, nullable-parameter facts, and fallback facts behind
  `ProgramModel`.
- `[x]` Emit fallback facts through the generated candidate metadata, so
  fallback use can be attributed to a function and to a specific regex-origin
  candidate when applicable.

### Phase B: Strengthen The Fixture Graph Builder

Status: `[~]` partially done

- `[x]` Add a typed `FixtureRequirement` model.
- `[x]` Add generic string fixtures for valid `char *` and `const char *`
  parameters.
- `[x]` Add explicit byte-buffer fixtures for valid `uint8_t *` and
  `const uint8_t *` parameters.
- `[x]` Extract simple `\valid_read(ptr + (0 .. n))` assumptions into typed
  byte-buffer requirements.
- `[x]` Extract simple `\valid(ptr + (0 .. n))` assumptions into typed writable
  byte-buffer requirements.
- `[x]` Extract simple object-path byte-buffer assumptions such as
  `\valid_read(obj->data + (0 .. obj->len - 1))` into typed requirements.
- `[x]` Retire the duplicate source-text assumption setup for
  `\valid_read(obj->data + (0 .. obj->len - 1))`; object-path buffer setup now
  belongs to typed fixture requirements.
- `[x]` Add general object-path constraints as typed requirements, not loose
  setup lines.
- `[x]` Add conflict detection for incompatible constraints.
- `[x]` Report fixture failure reasons in generated summaries.

### Phase C: Add Abstract Fact Propagation

Status: `[~]` partially done

- `[x]` Track branch facts on candidates.
- `[x]` Track path-sensitive direct post-state facts.
- `[x]` Track ownership summaries for consumed/transferred pointers.
- `[x]` Track nullness facts through selected paths.
- `[x]` Track scalar interval facts through selected paths.
- `[x]` Suppress unsafe old-state witnesses when a selected path makes a root
  pointer null.
- `[x]` Carry call-outcome facts from helper summaries.
- `[x]` Carry object-path facts through branch and helper-call candidates.
- `[x]` Carry ownership facts from helper summaries across call chains.

### Phase D: Build Generic State-Machine Analysis

Status: `[x]` done

- `[x]` Generate switch-case candidates from typed IR.
- `[x]` Generate path-specific facts for switch cases.
- `[x]` Build a state-transition graph from assignments to the switch selector.
- `[x]` Generate transition candidates from the graph.
- `[x]` Explain transitions in diagnostic metadata.
- `[x]` Support state machines spread across helper functions.

### Phase E: Build Helper-Effect Summaries

Status: `[~]` partially done

- `[x]` Infer simple helper success/failure conditions.
- `[x]` Infer simple helper side effects.
- `[x]` Let callers request a helper-success fixture.
- `[x]` Let callers request a helper-failure fixture.
- `[~]` Use helper summaries for queue, table, scheduler, and callback
  candidates without project-specific code.

### Phase F: Improve Diagnostic Reporting

Status: `[~]` partially done

- `[x]` Emit unproved diagnostic tests.
- `[x]` Emit quality summaries.
- `[ ]` Classify unproved diagnostics by likely cause.
- `[ ]` Report fixture construction failures separately from EVA proof
  failures.
- `[x]` Report fallback use per function and candidate.

## Acceptance Criteria

This architecture is successful when:

- Generic C constructs are shaped from AST/IR, not source regex.
- KLEVA can explain why it generated each candidate.
- KLEVA can explain why it did not generate or prove a candidate.
- Valid pointer assumptions never silently become `NULL`.
- String, buffer, struct, array, callback, and opaque pointer fixtures are
  handled through one fixture graph builder.
- State-machine shaping works for non-networking code.
- Project-specific knowledge is kept in explicit user rules/plugins.
- Trusted tests are proof-backed.
- Diagnostic tests expose proof gaps instead of hiding them.

## References

- Frama-C project: https://frama-c.com/
- Frama-C open-source repository: https://git.frama-c.com/pub/frama-c
- "Frama-C, A Software Analysis Perspective":
  https://arxiv.org/abs/1508.03898
