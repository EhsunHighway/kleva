# Augment Rules

Augment rules let users add source-derived test cases without editing KLEVA's
Python internals.

They are useful when ACSL contracts describe the public behavior, but the
implementation has extra branches worth probing:

- error paths
- switch cases
- boundary conditions
- helper-call paths
- state transitions visible in source

Rules are intentionally data-driven. Project-specific setup belongs in a rules
file, not in KLEVA core.

## Minimal Rule File

```yaml
rules:
  - name: receive_too_short
    when:
      function_pattern: "\\bint\\s+receive\\s*\\("
      pattern: "frame->len\\s*<\\s*HEADER_LEN"
    body:
      - "Frame *frame = frame_create(4);"
      - "__GUARD__(frame)"
      - "int out_ret = receive(frame);"
    outputs:
      - out_ret
    cleanup:
      - "frame_free(frame);"
```

Each matching rule becomes one generated KLEVA function entry.

## Rule Fields

### `name`

Required. The generated test-case name.

```yaml
name: receive_too_short
```

KLEVA uses this name for:

- KLEE harness name
- KLEE output directory
- generated unit-test function suffix

### `when.pattern`

Required. A Python regular expression matched against the implementation source.

```yaml
when:
  pattern: "ttl\\s*==\\s*0"
```

If the pattern is found, KLEVA adds the case.

### `when.function_pattern`

Optional. A second Python regular expression that must also match the source.
Use this to avoid adding a rule because the same branch text appears in an
unrelated function.

```yaml
when:
  function_pattern: "\\bint\\s+ip_receive\\s*\\("
  pattern: "ttl\\s*==\\s*0"
```

### `body`

Required for useful tests. A list of C statements used as the generated test
body.

```yaml
body:
  - "Packet *pkt = packet_create(64);"
  - "__GUARD__(pkt)"
  - "int out_ret = target(pkt);"
```

`__GUARD__(expr)` means the generated test is discarded if the guard is false.
It is useful for constructors and setup helpers.

### `outputs`

Variables that EVA should prove as singleton values.

```yaml
outputs:
  - out_ret
  - out_state
```

KLEVA only emits final unit-test assertions for values EVA proves.

### `cleanup`

C cleanup statements appended after the body.

```yaml
cleanup:
  - "object_free(obj);"
```

If the function under test takes ownership of an object, do not free that object
again in cleanup.

## Applying Rules Without YAML

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --rules rules.yaml \
  --mode all \
  --base-dir .
```

This path:

1. Synthesizes the base test plan in memory.
2. Applies rules in memory.
3. Runs KLEE/EVA.
4. Generates unit tests.

No module YAML file is required.

To inspect the final augmented plan:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --rules rules.yaml \
  --emit-yaml /tmp/module_augmented.yaml \
  --mode all \
  --base-dir .
```

## Applying Rules With YAML

```sh
kleva augment kleva/module.yaml \
  --rules rules.yaml \
  --out kleva/module_augmented.yaml
```

Then:

```sh
kleva all kleva/module_augmented.yaml --base-dir .
```

## Matching Behavior

Rules are conservative:

- If a pattern does not match, the rule is skipped.
- If multiple rules match, all matching rules are added.
- If a generated rule name already exists, the old matching entry is replaced.
- Rules do not prove anything by themselves; KLEE/EVA still validate the result.

## Design Rule

Keep project-specific knowledge in rule files.

Good:

```yaml
rules:
  - name: my_project_specific_case
    body:
      - "MyType *obj = my_project_create();"
```

Bad:

```python
# inside KLEVA core
if type_name == "MyType":
    emit_my_project_create()
```

KLEVA core should learn generic source and type patterns. Project setup belongs
to the user.

