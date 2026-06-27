# Helper Call Repair Rules

Helper call repair rules tell KLEVA how to shape inputs for a guarded helper
call found from the C AST.

Use them for code like:

```c
if (verify(input) != 0) {
    return -1;
}
```

KLEVA can detect this branch generically. It cannot always know what input state
makes `verify(input)` return success or failure. A helper call repair rule
provides that missing setup without putting project-specific helper names into
KLEVA itself.

## Rule File

```yaml
helper_call_rules:
  - callee: verify
    success_setup:
      - "{arg0}->value = 1;"
    failure_setup:
      - "{arg0}->value = 0;"
```

Fields:

- `callee`: helper function name as it appears in the call expression.
- `success_setup`: C setup lines for the candidate that tries to make the helper
  guard pass.
- `failure_setup`: C setup lines for the candidate that tries to make the helper
  guard fail.

Template placeholders:

- `{arg0}`: first helper-call argument
- `{arg1}`: second helper-call argument
- `{arg2}`: third helper-call argument
- `{callee}`: helper function name

Unknown argument placeholders are ignored rather than emitted into generated C.

## Use With `kleva run`

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --helper-rules helper-rules.yaml \
  --mode all \
  --base-dir .
```

Inspect the synthesized plan:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --helper-rules helper-rules.yaml \
  --emit-yaml /tmp/module.yaml \
  --mode klee \
  --base-dir .
```

## Use With `kleva synth`

```sh
kleva synth module.h \
  --source module.c \
  --include . \
  --helper-rules helper-rules.yaml \
  --out kleva/module.yaml
```

Then run the generated YAML:

```sh
kleva all kleva/module.yaml --base-dir .
```

## Difference From Augment Rules

Helper call repair rules attach setup to AST/IR-discovered helper-call
candidates.

Augment rules add whole extra test cases from source-pattern matches and full
body/output/cleanup templates.

Use helper call repair rules when KLEVA already finds the branch but needs setup
to make a helper predicate pass or fail. Use augment rules when you want to add
a separate scenario that KLEVA does not synthesize.
