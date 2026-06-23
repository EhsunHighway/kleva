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

- `byte-order`
- `casted-fields`
- `function-pointers`
- `loop-tables`
- `quantified-arrays`

Use `--shaping none` to turn shaping off.

## Output Files

By default, generated artifacts use paths from the synthesized plan:

- KLEE harnesses: `klee_build/harnesses/`
- KLEE outputs: `klee_build/klee_out_*`
- EVA probe: `eva/eva_<module>_kleva.c`
- Unit test: `unit/test_<module>_kleva.c`

`--base-dir` controls where those relative paths are resolved.

