# KLEVA

KLEVA is an automatic C unit-test generation tool for ACSL-annotated modules.
It uses KLEE to explore feasible inputs and Frama-C EVA to prove concrete
output values, then emits C tests with behavior-grounded assertion oracles
derived from the existing code, contracts, and analysis results.

The short version, with a local install:

```sh
kleva run module.h --source module.c --include . --mode all --base-dir .
```

Make your life easier and use Docker when you can. The Docker image bundles
KLEVA, KLEE, `ktest-tool`, LLVM tools, and Frama-C EVA, so you do not have to
line up the verification toolchain by hand before trying the tool.

```sh
docker pull <dockerhub-namespace>/kleva:latest
docker run --rm --ulimit='stack=-1:-1' -v "$PWD:/work" <dockerhub-namespace>/kleva:latest run module.h \
  --source module.c \
  --include . \
  --mode all \
  --base-dir .
```

Replace `<dockerhub-namespace>` with the Docker Hub namespace that owns the
published image.

## What KLEVA Does

KLEVA generates both test inputs and expected-output assertions. It runs a
five-step pipeline:

1. Generate KLEE harnesses from a C header, source file, and ACSL contracts.
2. Run KLEE to discover concrete path inputs.
3. Convert `.ktest` values into EVA probe functions.
4. Run Frama-C EVA to prove final output values.
5. Generate C unit tests with only proven assertions.

If EVA cannot prove an output value, KLEVA reports it instead of turning it into
a guessed oracle.

## Requirements

With Docker, you only need Docker.

For a native install, KLEVA is a Python package, but it drives external
verification tools. You need:

- Python 3.10 or newer
- `klee`
- `ktest-tool`
- `frama-c`
- LLVM `clang`
- LLVM `llvm-link`

On macOS, the practical dependency path is:

```sh
brew install klee
brew install opam gmp graphviz zmq
opam init --compiler 4.14.1
eval $(opam env)
opam install frama-c
```

KLEE, LLVM, and Frama-C versions are sensitive to each other. If your system
uses non-default paths, set:

```sh
export KLEE_INCLUDE=/path/to/klee/include
export KLEE_CLANG=/path/to/clang
export LLVM_LINK=/path/to/llvm-link
```

You can also override tool paths in a KLEVA YAML config.

## Installation

From this repository:

```sh
python3 -m pip install -e .
```

After installation:

```sh
kleva --help
kleva -help
kleva run --help
kleva all --help
```

For isolated CLI installs, `pipx` is a good fit:

```sh
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install .
```

## Homebrew

If a Homebrew tap is available, install KLEVA with:

```sh
brew tap <owner>/kleva
brew install kleva
```

Until then, install KLEVA with `pip` or `pipx` from this repository. The
external verification tools still need to be installed separately.

## Docker

Make your life easier and use Docker if you want the quickest working setup.
The image includes KLEVA, KLEE, `ktest-tool`, LLVM tools, and Frama-C EVA.

Use the published image from Docker Hub:

```sh
docker pull <dockerhub-namespace>/kleva:latest
docker run --rm --ulimit='stack=-1:-1' -v "$PWD:/work" <dockerhub-namespace>/kleva:latest run module.h \
  --source module.c \
  --include . \
  --mode all \
  --base-dir .
```

The mounted directory becomes `/work` inside the container. KLEVA writes its
generated harnesses, EVA probes, and unit tests back into that mounted project
directory.

Build the image from this repository:

```sh
docker build -t kleva:latest .
```

Check the CLI:

```sh
docker run --rm --ulimit='stack=-1:-1' kleva:latest --help
```

Check the bundled tools:

```sh
docker run --rm --ulimit='stack=-1:-1' --entrypoint bash kleva:latest -c \
  'klee --version && ktest-tool --help >/dev/null && frama-c -version && kleva --help >/dev/null'
```

Run KLEVA against a local C project by mounting it at `/work`:

```sh
docker run --rm --ulimit='stack=-1:-1' -v "$PWD:/work" kleva:latest run module.h \
  --source module.c \
  --include . \
  --mode all \
  --base-dir .
```

A small Docker smoke module lives in `docker/smoke`. After building the image,
you can run the full pipeline against it:

```sh
docker run --rm --ulimit='stack=-1:-1' -v "$PWD/docker/smoke:/work" kleva:latest run lucky.h \
  --source lucky.c \
  --include . \
  --mode all \
  --base-dir .
```

## No-YAML Workflow

Use `kleva run` when you want KLEVA to synthesize the test plan in memory and
run immediately:

```sh
kleva run path/to/module.h \
  --source path/to/module.c \
  --include path/to/include \
  --mode all \
  --base-dir .
```

Useful modes:

```sh
kleva run module.h --source module.c --include . --mode klee
kleva run module.h --source module.c --include . --mode gen
kleva run module.h --source module.c --include . --mode all
```

`--mode klee` generates and runs KLEE harnesses only.

`--mode gen` consumes existing KLEE outputs and runs EVA/unit generation.

`--mode all` runs both phases.

Export the synthesized plan for inspection:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --mode all \
  --emit-yaml /tmp/module.yaml
```

## YAML Workflow

Use `kleva synth` when you want to write and inspect a YAML plan first:

```sh
kleva synth path/to/module.h \
  --source path/to/module.c \
  --include path/to/include \
  --out kleva/module.yaml
```

Then run the pipeline:

```sh
kleva all kleva/module.yaml --base-dir .
```

Run phases separately:

```sh
kleva klee kleva/module.yaml --base-dir .
kleva gen  kleva/module.yaml --base-dir .
```

## Augment Rules

KLEVA can add source-shaped edge cases from user-provided rules:

```sh
kleva run module.h \
  --source module.c \
  --include . \
  --rules rules.yaml \
  --mode all \
  --base-dir .
```

The same rules can be applied to an existing YAML file:

```sh
kleva augment kleva/module.yaml \
  --rules rules.yaml \
  --out kleva/module_augmented.yaml
```

Augment rules are data, not Python plugins. KLEVA core should stay generic;
project-specific setup belongs in user rule files.

## Command Summary

```sh
kleva synth   module.h [--source module.c] [--include DIR] [--out FILE]
kleva run     module.h [--source module.c] [--include DIR] [--mode all]
kleva augment module.yaml [--rules rules.yaml] [--out FILE]
kleva klee    module.yaml --base-dir .
kleva gen     module.yaml --base-dir .
kleva all     module.yaml --base-dir .
kleva refine  module.yaml --base-dir .
```

Every command supports `-h`, `--help`, and `-help`.

## Documentation

- [docs/usage.md](docs/usage.md)
- [docs/augment-rules.md](docs/augment-rules.md)
- [docs/modules.md](docs/modules.md)
