import tempfile
import unittest
from pathlib import Path

from kleva.source_discovery import (
    collect_source_include_headers,
    collect_visible_headers,
    dedupe_paths,
    source_include_names,
    suggest_extra_sources,
)


class SourceDiscoveryTests(unittest.TestCase):
    def test_collects_recursive_quoted_headers_and_suggests_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_h = root / "main.h"
            dep_h = root / "dep.h"
            dep_c = root / "dep.c"
            main_c = root / "main.c"

            main_h.write_text('#include "dep.h"\nint main_api(void);\n')
            dep_h.write_text("int dep_api(void);\n")
            dep_c.write_text('#include "dep.h"\n')
            main_c.write_text('#include "main.h"\n#include "dep.h"\n')

            visible = "\n".join(collect_visible_headers(main_h))
            self.assertIn("main_api", visible)
            self.assertIn("dep_api", visible)

            source_visible = "\n".join(collect_source_include_headers(main_c))
            self.assertIn("dep_api", source_visible)

            self.assertEqual(source_include_names(main_c), ["main.h", "dep.h"])
            self.assertTrue(any(path.endswith("dep.c") for path in suggest_extra_sources(main_h, [], str(main_c))))

    def test_dedupes_paths_by_resolved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "file.c"
            p.write_text("")

            self.assertEqual(dedupe_paths([str(p), str(p.resolve())]), [str(p)])


if __name__ == "__main__":
    unittest.main()
