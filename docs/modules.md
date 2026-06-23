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

Touch this module when changing the test-plan schema.

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

Important function:

- `parse_acsl(header_path)`

This module intentionally extracts a practical subset of ACSL rather than
implementing the whole ACSL language.

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
- Source-shaped branch candidates.

Shaping features are controlled by:

- `--shaping`
- `--no-shaping`

Touch this module when improving automatic test-plan generation.

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

Important function:

- `build_recipes_for_function(...)`

Touch this module when changing how symbolic inputs become concrete C code.

## `recipe.py`

Recipe data model and guard expansion.

Important dataclass:

- `Recipe`

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

