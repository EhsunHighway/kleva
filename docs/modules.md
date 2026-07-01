# Module Reference

This document describes the current KLEVA package structure.

KLEVA is organized around one data flow:

```text
C header + ACSL
  -> synthesized test plan
  -> KLEE harnesses
  -> .ktest files
  -> concrete recipes
  -> EVA probes
  -> singleton values
  -> C unit tests
```

YAML can store the test plan, but the same plan can also stay in memory through
`kleva run`.

## Package Layout

```text
src/kleva/
  acsl.py
  augment.py
  builder.py
  cli.py
  codegen.py
  config.py
  eva.py
  kernel/
  klee.py
  ktest.py
  pipeline.py
  recipe.py
  refiner.py
  synth.py
```

## `cli.py`

Command-line entry point for the `kleva` executable.

Main commands:

- `kleva synth`: generate a YAML plan from a header/source pair.
- `kleva run`: synthesize the plan in memory and run KLEE/EVA.
- `kleva augment`: apply source-derived rule cases to a YAML plan.
- `kleva klee`: run KLEE phase from YAML.
- `kleva gen`: run EVA/unit-generation phase from YAML.
- `kleva all`: run both KLEE and EVA/unit-generation from YAML.
- `kleva refine`: rebuild a YAML plan from generated output.

Important responsibilities:

- Parse CLI arguments.
- Choose YAML-backed or in-memory execution.
- Call `synth`, `augment`, and `pipeline` modules.
- Print final pipeline results.

Touch this module when adding a user-facing command or option.

## `config.py`

Typed configuration model for KLEVA test plans.

Important dataclasses:

- `Bounds`
- `InputSpec`
- `KleeSettings`
- `FunctionSpec`
- `ModuleConfig`

Important functions:

- `load_config(path)`: parse YAML from a file.
- `load_config_text(text)`: parse YAML text from memory.
- `resolve_klee_clang(...)`
- `resolve_llvm_link(...)`
- `resolve_klee_include(...)`

The config model is the shared API between YAML mode and no-YAML mode. `kleva
run` generates YAML text internally, parses it with `load_config_text`, and then
uses the same `ModuleConfig` path as YAML mode.

Candidate entries may carry source metadata:

- `source_location`
- `target_branch`
- `candidate_origin`
- `candidate_facts`

`candidate_facts` is a machine-readable explanation of the candidate goal, for
example a branch fact such as `ctx->state case 1` or a call outcome such as
`prepare equals_-1 success`.

Touch this module when changing the test-plan schema.

## `bodygen.py`

Builds concrete C bodies for KLEE harnesses, EVA probes, trusted unit tests,
and diagnostic unit tests.

Important responsibilities:

- construct arguments and fixtures from ACSL, IR facts, and fixture
  requirements
- emit meaningful post-call witnesses from ACSL old-state contracts, typed IR
  assignments, callback witnesses, and helper side effects
- ignore post-call witnesses whose expected value depends on a callee-local
  variable that is not visible in the generated harness
- mark missing observables with `oracle-missing:` instead of generating weak
  `out_ok` placeholders

Touch this module when changing how candidates become concrete C setup code or
when adding a new kind of observable.

## `codegen.py`

Writes probe drivers, trusted unit tests, KLEE harnesses, and unproved
diagnostic artifacts.

Trusted tests only contain EVA-proven singleton assertions. Candidates whose
outputs are not fully proved are kept out of trusted tests and, when requested,
are emitted separately with reason categories:

- `weak_fixture`
- `weak_oracle`
- `missing_acsl`
- `eva_imprecision`
- `implementation_bug`
- `timeout`

Touch this module when changing trusted-vs-diagnostic policy, unproved reports,
or the final C file layout.

## `shaping/ir_helper_effects.py`

Generic helper-effect summaries from typed IR.

Important dataclasses:

- `HelperEffectSummary`
- `HelperSideEffect`

The summary layer infers helper behavior that caller candidates can request:

- success and failure fixture setup from helper guard conditions
- field assignment side effects
- array-slot assignment side effects
- generic call side effects
- post-state facts
- ownership transfer or consumption facts

This module must stay domain-neutral. It should describe C shapes such as
assignments, calls, and guarded returns, not names such as TCP, packet, event,
or queue.

## `shaping/ir_switches.py`

Generic switch and state-machine shaping from typed IR.

Important function:

- `state_switch_candidates_from_ir(...)`

This shaper recognizes enum-like control fields by shape:

- a switch selector such as `obj->state`
- cases for possible source states
- assignments back to the same selector as transitions
- guard conditions inside cases
- helper calls whose summaries assign the selector

Generated transition candidates include `transition` candidate facts with the
selector, source state, target state, and optional guard/helper explanation.
The shaper must not know TCP, scheduler, or protocol names.

## `shaping/diversity.py`

Generic curated value/content diversity shaper.

Important function:

- `curated_diversity_candidates(...)`

This shaper adds a small number of optional candidates for API input variation:

- selected scalar boundary values, one scalar parameter at a time
- byte-buffer content patterns:
  - all zero
  - all `0xff`
  - first byte set
- length-like boundary values such as `0`, `1`, and `2`

It deliberately avoids Cartesian-product generation. Each candidate changes one
input dimension and carries `diversity` candidate facts, so KLEE/EVA still
decide whether the candidate becomes a trusted test.

Scalar diversity is represented with concrete call-argument overrides. Do not
emit setup assignments for these values; setup assignments allow KLEE to keep
the original argument symbolic and can multiply recipes. Unknown typedefs and
function-pointer parameters are not scalar-diversity targets.

Scalar diversity is also contract-aware. If the active ACSL assumptions require
`len > 0`, `n != 0`, `x <= 8`, or the flipped equivalent such as `0 < len`,
the shaper filters out boundary values that violate those assumptions. Invalid
contract exploration belongs in a separate diagnostic mode, not in normal
trusted-path diversity.

## `kernel/`

Static-analysis kernel facade.

Important dataclasses:

- `ProgramInput`
- `ProgramModel`

Important function:

- `build_program_model(...)`

The kernel gathers the shared facts that later KLEVA stages should consume:

- public functions from the header
- ACSL contracts
- visible source text for explicit fallback paths
- Clang-derived function IR
- Clang-derived function declarations
- Clang-derived type catalog
- fallback diagnostics

`ProgramModel` is also the query facade for synthesis code. Use methods such
as `function_ir`, `function_decl`, `accepts_null_param`, and
`fallback_fact_dicts` instead of reaching directly into parser helpers or
source-text fallback helpers from orchestration code.

This package is the boundary between raw input files and analysis modules.
Shapers, fixture construction, and synthesis orchestration should ask the
kernel for typed facts instead of independently parsing source files.

Touch this package when adding shared front-end facts or changing how KLEVA
decides whether it is using typed IR or source-text fallback behavior.

## `acsl.py`

ACSL parser for C header annotations.

Supported contract pieces:

- `behavior <name>:`
- `assumes`
- `ensures`
- `assigns`
- `complete behaviors`
- `disjoint behaviors`

Important dataclasses:

- `ACSLBehavior`
- `ACSLSpec`
- `AcslParser`
- `ScannerAcslParser`
- `RegexAcslParser` compatibility alias

Important function:

- `parse_acsl(header_path)`

This module intentionally extracts a practical subset of ACSL rather than
implementing the whole ACSL language. The current parser is scanner-backed and
is isolated behind `AcslParser` / `ScannerAcslParser`. Synthesis accepts an
`AcslParser`, so a future fuller ACSL parser can replace the scanner without
changing the test-plan generator.

Touch this module when KLEVA needs to understand more contract syntax.

## `synth.py`

ACSL-aware test-plan synthesizer.

It reads:

- C function declarations from a header.
- ACSL behaviors from comments.
- visible C types and helper functions from headers/sources.

It emits:

- YAML text representing a complete `ModuleConfig`.

Main public functions:

- `generate_yaml_from_header(...)`
- `run_synth(...)`

Main concepts:

- Null behavior generation.
- Valid pointer setup.
- Constructor/free inference.
- Function-pointer stub generation.
- Quantified-array shaping.
- Casted-struct field shaping.
- Byte-order-aware assignments.
- Loop/table match and miss candidates.
- AST/IR-shaped branch candidates.
- Optional regex fallback branch candidates for compatibility.

Shaping features are controlled by:

- `--shaping`
- `--no-shaping`

Touch this module when improving automatic test-plan generation.

## `compat/source_fallbacks.py`

Explicit compatibility boundary for older source-text helpers.

The default synthesis path should prefer:

- Clang header declarations.
- Clang-derived type/function metadata.
- Typed implementation IR.
- `ScannerAcslParser` for ACSL comments.

`compat/source_fallbacks.py` wraps the older source-text parsers and scanners
with names such as `fallback_parse_header`, `fallback_function_body`, and
`fallback_build_type_catalog`. Use these only when IR is disabled, IR extraction
fails, or `regex-fallbacks` is explicitly requested.

Generated YAML reports fallback use in the header:

- `# Fallbacks: none`
- `# Fallbacks: used`

When fallback is used, `kleva synth` prints a warning, `--ir-diagnostics`
records `source-fallback` entries, and emitted test-plan entries include
fallback `candidate_facts` so fallback use is visible per function or candidate.

## `augment.py`

Data-driven source augmentation.

It applies user-provided rules to add extra cases based on implementation
patterns. Rules are regular-expression matches plus generated C body/output/
cleanup snippets.

Important dataclasses:

- `AugmentedCase`
- `AugmentRule`

Important functions:

- `augment_yaml_text(...)`: apply rules to YAML text in memory.
- `augment_yaml(...)`: apply rules to a YAML file.
- `run_augment(...)`: CLI wrapper for `kleva augment`.

This module should stay generic. Project-specific setup belongs in user rule
files, not Python code.

Touch this module when changing the augment rule language.

## `pipeline.py`

Pipeline orchestration.

Main phases:

1. `run_klee_phase(...)`
2. `run_pipeline(...)`

`run_klee_phase`:

- Writes KLEE harnesses.
- Compiles harness/source bitcode.
- Runs KLEE for each function spec.

`run_pipeline`:

- Parses `.ktest` files into recipes.
- Writes EVA probe drivers.
- Runs Frama-C EVA.
- Parses singleton values.
- Writes generated C unit tests.

Touch this module when changing phase order or high-level pipeline behavior.

## `klee.py`

KLEE integration.

Responsibilities:

- Compile C files to LLVM bitcode with `clang -emit-llvm`.
- Link harness and source bitcode with `llvm-link`.
- Run `klee`.
- Manage KLEE output directories.

Important function:

- `run_klee_for_function(...)`

This module owns tool invocation for symbolic execution. It does not decide
what tests should exist; it executes the `FunctionSpec` plan it receives.

Touch this module when changing compiler/linker/KLEE invocation behavior.

## `ktest.py`

Parser for KLEE `.ktest` files.

Responsibilities:

- Run `ktest-tool`.
- Parse symbolic object names, sizes, and bytes.
- Expose scalar values as little-endian integers.

Important dataclass:

- `KTestObject`

Important function:

- `parse_ktest(path, ktest_tool)`

Touch this module when KLEE output parsing changes.

## `builder.py`

Converts KLEE outputs into concrete recipes.

Input:

- `FunctionSpec`
- `.ktest` objects

Output:

- `Recipe`

Responsibilities:

- Map symbolic objects to C declarations.
- Apply scalar bounds.
- Apply `skip_if`.
- Build array declarations.
- Produce one recipe per accepted KLEE test vector.
- Preserve candidate metadata and semantic facts for diagnostics.

Important function:

- `build_recipes_for_function(...)`

Touch this module when changing how symbolic inputs become concrete C code.

## `recipe.py`

Recipe data model and guard expansion.

Important dataclass:

- `Recipe`

Recipes preserve candidate source metadata and semantic facts so generated
unit-test diagnostics can explain why an optional candidate was created.

Important helper:

- `expand_guard(...)`

Guard markers:

- `__GUARD__(expr)`
- `__GUARD_WITH_CLEANUP__(expr, cleanup_stmt)`

Probe behavior:

- failed guards return early.

Unit-test behavior:

- guards become `assert(...)`.

Touch this module when changing guard syntax or recipe representation.

## `codegen.py`

C code generator.

Generated outputs:

- KLEE harness C files.
- EVA probe driver.
- standalone EVA probe files.
- generated unit test C file.

Important functions:

- `write_klee_harness(...)`
- `write_probe_driver(...)`
- `write_probe_standalone(...)`
- `write_unit_tests(...)`

Important rule:

- Unit tests only receive assertions for EVA-proven singleton values.

If a requested output is not proven by EVA, unit-test generation reports it as
unproven instead of inventing an expected value.

Touch this module when changing emitted C style or oracle generation.

## `eva.py`

Frama-C EVA integration.

Responsibilities:

- Run `frama-c -eva`.
- Pass include paths, source files, precision, and extra flags.
- Parse final-state singleton values from EVA logs.

Important functions:

- `run_eva(...)`
- `parse_singletons(...)`

EVA singleton values are the source of generated unit-test oracles.

Touch this module when changing Frama-C invocation or singleton parsing.

## `quality_report.py`

Generated-test quality reporting.

It scans generated unit-test artifacts and reports:

- trusted tests
- trusted assertions
- EVA-proven assertions
- unproved diagnostics
- skipped candidates
- runtime from summary JSON files

It also provides `compare_generated_tests_by_api(...)` for comparing older and
newer generated unit files by explicit public API names. API names are supplied
by the caller instead of guessed from underscores in test names.

## `refiner.py`

YAML refinement from generated output.

Responsibilities:

- Read generated unit tests.
- Read KLEE output directories.
- Read the existing YAML plan.
- Extract working generated bodies.
- Produce a cleaner reusable YAML plan.

Important function:

- `run_refine(...)`

This command is useful when a generated test file contains a better concrete
body than the original synthesized YAML.

Touch this module when improving reverse-engineering from generated tests back
into reusable plans.

## `__init__.py`

Package metadata.

Currently exposes:

- `__version__`

## Cross-Module Data Flow

### No-YAML Flow

```text
cli.py
  -> synth.generate_yaml_from_header(...)
  -> optional augment.augment_yaml_text(...)
  -> config.load_config_text(...)
  -> pipeline.run_klee_phase(...)
  -> pipeline.run_pipeline(...)
```

### YAML Flow

```text
cli.py
  -> config.load_config(...)
  -> pipeline.run_klee_phase(...)
  -> pipeline.run_pipeline(...)
```

### KLEE Phase

```text
pipeline.py
  -> codegen.write_klee_harness(...)
  -> klee.run_klee_for_function(...)
```

### EVA and Unit-Test Phase

```text
pipeline.py
  -> builder.build_recipes_for_function(...)
  -> codegen.write_probe_standalone(...)
  -> eva.run_eva(...)
  -> eva.parse_singletons(...)
  -> codegen.write_unit_tests(...)
```

## Extension Points

Add a new CLI option:

- `cli.py`

Add new ACSL syntax support:

- `acsl.py`
- possibly `synth.py`

Improve automatic test generation:

- `synth.py`

Add data-driven edge-case rules:

- rule YAML files
- `augment.py` only if the rule language itself changes

Change KLEE invocation:

- `klee.py`

Change EVA invocation or parsing:

- `eva.py`

Change generated C formatting:

- `codegen.py`

Change the test-plan schema:

- `config.py`
- all producers/consumers of affected fields
