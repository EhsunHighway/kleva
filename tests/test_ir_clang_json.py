import tempfile
import unittest
from pathlib import Path

from kleva.ir.clang_json import parse_translation_unit
from kleva.ir.model import BinaryOp, FieldAccess, IfStmt, SwitchStmt, VarRef


class ClangJsonIrTests(unittest.TestCase):
    def test_extracts_if_and_switch_facts_from_real_clang_ast(self):
        source = """
            typedef struct Obj { int state; int count; } Obj;
            int run(Obj *obj, int x) {
                if (!obj || x == 0) return -1;
                switch (obj->state) {
                    case 1: return obj->count;
                    case 2: return 2;
                    default: return 0;
                }
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        run = funcs["run"]
        if_stmt = next(stmt for stmt in run.statements if isinstance(stmt, IfStmt))
        switch_stmt = next(stmt for stmt in run.statements if isinstance(stmt, SwitchStmt))

        self.assertIsInstance(if_stmt.condition, BinaryOp)
        self.assertEqual(if_stmt.condition.op, "||")
        self.assertIsInstance(switch_stmt.selector, FieldAccess)
        self.assertEqual(switch_stmt.selector.field, "state")
        self.assertIsInstance(switch_stmt.selector.base, VarRef)
        self.assertEqual(switch_stmt.selector.base.name, "obj")
        self.assertEqual([case.value for case in switch_stmt.cases], [1, 2])


if __name__ == "__main__":
    unittest.main()
