import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kleva.config import FunctionSpec, KleeSettings
from kleva.klee import run_klee_for_function


class KleeRunnerTests(unittest.TestCase):
    def test_source_included_mode_skips_primary_source_bitcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness_dir = root / "klee_build" / "harnesses"
            harness_dir.mkdir(parents=True)
            (harness_dir / "klee_internal_valid.c").write_text("int main(void) { return 0; }\n")
            spec = FunctionSpec(
                name="internal_valid",
                ktest_dir="klee_build/klee_out_internal_valid",
                inputs=[],
                body=[],
                outputs=[],
                cleanup=[],
            )
            settings = KleeSettings(
                klee="klee",
                klee_clang="clang",
                llvm_link="llvm-link",
                klee_include="/include",
                output_base="klee_build",
            )

            compiled: list[str] = []
            linked: list[tuple[str, ...]] = []

            def fake_compile(clang, src_c, out_bc, *args, **kwargs):
                compiled.append(Path(src_c).name)
                Path(out_bc).parent.mkdir(parents=True, exist_ok=True)
                Path(out_bc).write_text("bc")

            def fake_link(llvm_link, *bc_files, out, verbose=True):
                linked.append(tuple(Path(path).name for path in bc_files))
                Path(out).write_text("linked")

            with patch("kleva.klee.compile_to_bc", side_effect=fake_compile), \
                 patch("kleva.klee.link_bitcode", side_effect=fake_link), \
                 patch("kleva.klee.run_klee"):
                run_klee_for_function(
                    spec,
                    settings,
                    str(root / "module.c"),
                    str(root),
                    extra_sources=[str(root / "dep.c")],
                    source_included=True,
                    base_dir=str(root),
                )

        self.assertNotIn("module.c", compiled)
        self.assertIn("dep.c", compiled)
        self.assertIn("klee_internal_valid.c", compiled)
        self.assertEqual(linked, [("klee_internal_valid.bc", "dep_source.bc")])


if __name__ == "__main__":
    unittest.main()
