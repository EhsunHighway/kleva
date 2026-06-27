# KLEVA Usage

This document shows the practical ways to run KLEVA.

## Mental Model

KLEVA has two layers:

- A test-plan layer: functions, harness bodies, outputs, cleanup, tool paths.
- A pipeline layer: KLEE, `.ktest` parsing, EVA, generated unit tests.

The test plan can live in memory or in YAML.

Use no-YAML mode when you want the shortest path:

```sh
kleva run module.h --source module.c --include . --mode all --base-dir .
```

Use YAML mode when you want to inspect, edit, version, or share the generated
plan:

```sh
kleva synth module.h --source module.c --include . --out kleva/module.yaml
kleva all kleva/module.yaml --base-dir .
```

The generated tests should be the same if both workflows receive the same
inputs and rules.

## No-YAML Mode

Run the whole pipeline:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --mode all \
  --base-dir .
```

Run KLEE only:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --mode klee \
  --base-dir .
```

Run EVA/unit generation only after KLEE outputs already exist:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --mode gen \
  --base-dir .
```

Export the in-memory plan for inspection:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --mode all \
  --base-dir . \
  --emit-yaml /tmp/module.yaml
```

## YAML Mode

Generate a YAML plan:

```sh
kleva synth module.h \
  --source module.c \
  --include . \
  --out kleva/module.yaml
```

Run the full pipeline:

```sh
kleva all kleva/module.yaml --base-dir .
```

Run phases separately:

```sh
kleva klee kleva/module.yaml --base-dir .
kleva gen  kleva/module.yaml --base-dir .
```

## Include and Source Arguments

Most real modules need include directories and linked sources:

```sh
kleva run module.h \
  --source module.c \
  --include include \
  --extra-include common \
  --extra-include dependencies \
  --extra-source dependencies/foo.c \
  --extra-source dependencies/bar.c \
  --mode all \
  --base-dir .
```

Repeat `--extra-include` and `--extra-source` as needed.

## Augment Rules in No-YAML Mode

Rules can be applied before the in-memory plan is executed:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --rules rules.yaml \
  --mode all \
  --base-dir .
```

With `--emit-yaml`, the exported YAML is the final augmented plan:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --rules rules.yaml \
  --emit-yaml /tmp/module_augmented.yaml \
  --mode all \
  --base-dir .
```

## Augment Rules in YAML Mode

Apply rules to an existing YAML plan:

```sh
kleva augment kleva/module.yaml \
  --rules rules.yaml \
  --out kleva/module_augmented.yaml
```

Then run it:

```sh
kleva all kleva/module_augmented.yaml --base-dir .
```

## Helper Call Repair Rules

Helper call repair rules shape candidates around guarded helper calls discovered
from the C AST, such as:

```c
if (verify(input) != 0) return -1;
```

Use them when KLEVA finds the branch but needs explicit setup to make the helper
call pass or fail:

```yaml
helper_call_rules:
  - callee: verify
    success_setup:
      - "{arg0}->value = 1;"
    failure_setup:
      - "{arg0}->value = 0;"
```

Run with helper rules:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --helper-rules helper-rules.yaml \
  --mode all \
  --base-dir .
```

Or synthesize a YAML plan with helper rules:

```sh
kleva synth module.h \
  --source module.c \
  --include . \
  --helper-rules helper-rules.yaml \
  --out kleva/module.yaml
```

More detail: [helper-call-rules.md](helper-call-rules.md).

## Shaping Features

`kleva synth` and `kleva run` support branch/input shaping features.

Enable only selected shapers:

```sh
kleva run module.h --source module.c --include . --shaping loop-tables --mode all
```

Disable selected default shapers:

```sh
kleva run module.h --source module.c --include . --no-shaping function-pointers --mode all
```

Current shapers:

- `branch-conditions`
- `byte-order`
- `callee-success`
- `casted-fields`
- `fallback-lookups`
- `function-pointers`
- `loop-tables`
- `parser-headers`
- `regex-fallbacks`
- `quantified-arrays`
- `state-switches`

Use `--shaping none` to turn shaping off.

By default, KLEVA enables the AST/IR shapers and leaves
`regex-fallbacks` off. Use `--shaping all` or explicitly include
`--shaping regex-fallbacks` when you need the older text/regex fallback
shapers for compatibility.

Generated plans include a header comment such as `# Fallbacks: none` or
`# Fallbacks: used`. When fallback is used because IR is disabled, IR
extraction failed, or `regex-fallbacks` was requested, `kleva synth` also prints
a warning and records the reason in `--ir-diagnostics` output.

## Coverage Candidate Report

KLEVA can render an external coverage-fact file as a candidate report:

```sh
kleva coverage-report coverage-facts.yaml --out coverage-report.md
```

The command is report-only. It does not run gcov/gcovr and does not feed
coverage back into synthesis.

The report also includes a conservative `Regex Fallback Retirement` section.
It reports `ready` only when all mapped candidates are IR-origin, proven, and
covered, with no unknown-origin candidates and no uncovered branch without a
candidate.

Input shape:

```yaml
candidates:
  - name: case_open
    source_location: src/run.c:42:5
    target_branch: switch ctx->state case 1
    proven: true
    origin: ir
    candidate_facts:
      - kind: branch
        target: ctx->state
        relation: case
        value: "1"
branches:
  - source_location: src/run.c:42:5
    target_branch: switch ctx->state case 1
    covered: true
```

## Output Files

By default, generated artifacts use paths from the synthesized plan:

- KLEE harnesses: `klee_build/harnesses/`
- KLEE outputs: `klee_build/klee_out_*`
- EVA probe: `eva/eva_<module>_kleva.c`
- Unit test: `unit/test_<module>_kleva.c`

`--base-dir` controls where those relative paths are resolved.
