import tempfile
import unittest
from pathlib import Path

from kleva.ir.clang_json import (
    parse_function_decl_map,
    parse_header_function_decls,
    parse_translation_unit,
    parse_translation_unit_with_decls,
    parse_type_catalog,
)
from kleva.ir.model import (
    AssignmentStmt,
    AddressOf,
    ArraySubscript,
    BinaryOp,
    CallExpr,
    CastExpr,
    DeclarationStmt,
    Dereference,
    ExprStmt,
    FieldAccess,
    IfStmt,
    LoopStmt,
    ReturnStmt,
    SwitchStmt,
    VarRef,
)


class ClangJsonIrTests(unittest.TestCase):
    def test_extracts_public_header_declarations_from_target_header(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dep = root / "dep.h"
            header = root / "api.h"
            dep.write_text("int dep_helper(int value);\n")
            header.write_text(
                """
#include "dep.h"
typedef struct Item Item;
Item *make_item(const int size, Item **out);
static int private_helper(Item *item);
""",
                encoding="utf-8",
            )

            funcs = parse_header_function_decls(header, [str(root)])

        self.assertEqual([func.name for func in funcs], ["make_item"])
        self.assertTrue(funcs[0].return_is_pointer)
        self.assertEqual(funcs[0].return_base, "Item")
        self.assertEqual(funcs[0].params[0].name, "size")
        self.assertEqual(funcs[0].params[0].base_type, "int")
        self.assertTrue(funcs[0].params[0].is_const)
        self.assertEqual(funcs[0].params[1].name, "out")
        self.assertEqual(funcs[0].params[1].pointer_depth, 2)

    def test_extracts_function_declarations_from_real_clang_ast(self):
        source = """
            typedef struct Item { int value; } Item;
            Item *make_item(const int size, Item **out);
            static int helper(Item *item) {
                return item ? item->value : 0;
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            decls = parse_function_decl_map(src)

        self.assertIn("make_item", decls)
        self.assertIn("helper", decls)
        self.assertTrue(decls["make_item"].return_is_pointer)
        self.assertEqual(decls["make_item"].return_base, "Item")
        self.assertEqual(decls["make_item"].params[0].name, "size")
        self.assertEqual(decls["make_item"].params[0].base_type, "int")
        self.assertTrue(decls["make_item"].params[0].is_const)
        self.assertEqual(decls["make_item"].params[1].name, "out")
        self.assertEqual(decls["make_item"].params[1].pointer_depth, 2)
        self.assertEqual(decls["helper"].params[0].base_type, "Item")

    def test_extracts_type_catalog_from_real_clang_ast(self):
        source = """
            typedef void (*RecvFn)(int value, void *ctx);
            typedef struct Entry {
                int valid;
                RecvFn recv;
            } Entry;
            typedef struct Opaque Opaque;
            struct Plain {
                Entry *entry;
            };
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            catalog = parse_type_catalog(src)

        self.assertIn("Entry", catalog.complete_structs)
        self.assertIn("Plain", catalog.complete_structs)
        self.assertIn("Opaque", catalog.opaque_structs)
        self.assertEqual(catalog.field_type("Entry", "valid").base_type, "int")
        self.assertEqual(catalog.field_type("Entry", "recv").base_type, "RecvFn")
        self.assertEqual(catalog.field_type("Plain", "entry").base_type, "Entry")
        self.assertTrue(catalog.field_type("Plain", "entry").is_pointer)
        self.assertEqual(catalog.function_pointer("RecvFn").return_type, "void")
        self.assertEqual(catalog.function_pointer("RecvFn").params[0].base_type, "int")
        self.assertEqual(catalog.function_pointer("RecvFn").params[1].base_type, "void")
        self.assertTrue(catalog.function_pointer("RecvFn").params[1].is_pointer)

    def test_parse_translation_unit_with_decls_reuses_one_clang_ast(self):
        source = """
            int helper(int value);
            int run(int value) {
                return helper(value);
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs, decls = parse_translation_unit_with_decls(src)

        self.assertIn("run", funcs)
        self.assertIn("helper", decls)
        self.assertEqual(decls["helper"].params[0].name, "value")

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
        self.assertIsNotNone(if_stmt.loc)
        self.assertEqual(if_stmt.loc.line, 4)
        self.assertTrue(any(isinstance(stmt, ReturnStmt) for stmt in if_stmt.body))
        self.assertIsInstance(switch_stmt.selector, FieldAccess)
        self.assertIsNotNone(switch_stmt.loc)
        self.assertEqual(switch_stmt.loc.line, 5)
        self.assertEqual(switch_stmt.selector.field, "state")
        self.assertIsInstance(switch_stmt.selector.base, VarRef)
        self.assertEqual(switch_stmt.selector.base.name, "obj")
        self.assertEqual([case.value for case in switch_stmt.cases], [1, 2])
        self.assertTrue(switch_stmt.has_default)
        self.assertTrue(any(isinstance(stmt, ReturnStmt) for stmt in switch_stmt.body))
        self.assertTrue(all(any(isinstance(stmt, ReturnStmt) for stmt in case.body) for case in switch_stmt.cases))
        self.assertTrue(any(isinstance(stmt, ReturnStmt) for stmt in switch_stmt.default_body))

    def test_extracts_enum_switch_case_names_from_real_clang_ast(self):
        source = """
            typedef enum State { STATE_INIT = 1, STATE_DONE = 2 } State;
            typedef struct Obj { State state; } Obj;
            int run(Obj *obj) {
                switch (obj->state) {
                    case STATE_INIT: return 1;
                    case STATE_DONE: return 2;
                }
                return 0;
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        switch_stmt = next(stmt for stmt in funcs["run"].statements if isinstance(stmt, SwitchStmt))

        self.assertEqual([case.value for case in switch_stmt.cases], ["STATE_INIT", "STATE_DONE"])

    def test_extracts_direct_call_statements_from_real_clang_ast(self):
        source = """
            void free(void *);
            void release(void *p) {
                if (!p) return;
                free(p);
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        release = funcs["release"]
        calls = [
            stmt.expr for stmt in release.statements
            if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr)
        ]

        self.assertEqual([call.callee for call in calls], ["free"])
        self.assertIsInstance(calls[0].args[0], VarRef)
        self.assertEqual(calls[0].args[0].name, "p")

    def test_extracts_call_result_and_argument_types_from_real_clang_ast(self):
        source = """
            typedef struct Item { int value; } Item;
            Item *make_item(int size);
            Item *build(int size) {
                return make_item(size);
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        build = funcs["build"]
        ret = next(stmt for stmt in build.statements if isinstance(stmt, ReturnStmt))

        self.assertIsInstance(ret.value, CallExpr)
        self.assertEqual(ret.value.c_type, "Item *")
        self.assertEqual(ret.value.args[0].c_type, "int")

    def test_extracts_assignment_and_return_statements_from_real_clang_ast(self):
        source = """
            typedef struct Owner { void *slot; } Owner;
            int store(Owner *owner, void *item) {
                owner->slot = item;
                return 0;
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        store = funcs["store"]
        assignment = next(stmt for stmt in store.statements if isinstance(stmt, AssignmentStmt))
        ret = next(stmt for stmt in store.statements if isinstance(stmt, ReturnStmt))

        self.assertIsInstance(assignment.target, FieldAccess)
        self.assertEqual(assignment.target.field, "slot")
        self.assertEqual(assignment.target.c_type, "void *")
        self.assertIsInstance(assignment.value, VarRef)
        self.assertEqual(assignment.value.name, "item")
        self.assertEqual(assignment.value.c_type, "void *")
        self.assertIsNotNone(ret.value)
        self.assertEqual(ret.value.c_type, "int")

    def test_extracts_local_declarations_from_real_clang_ast(self):
        source = """
            int setup(int x) {
                int total = x;
                void *slot = 0;
                return total;
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        setup = funcs["setup"]
        decls = [stmt for stmt in setup.statements if isinstance(stmt, DeclarationStmt)]

        self.assertEqual([(decl.name, decl.c_type) for decl in decls], [
            ("total", "int"),
            ("slot", "void *"),
        ])
        self.assertIsInstance(decls[0].init, VarRef)
        self.assertEqual(decls[0].init.name, "x")
        self.assertIsNotNone(decls[0].loc)
        self.assertEqual(decls[0].loc.line, 3)

    def test_extracts_address_of_and_dereference_from_real_clang_ast(self):
        source = """
            int readp(int *p) {
                int *q = &p[0];
                return *q;
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        readp = funcs["readp"]
        decl = next(stmt for stmt in readp.statements if isinstance(stmt, DeclarationStmt))
        ret = next(stmt for stmt in readp.statements if isinstance(stmt, ReturnStmt))

        self.assertIsInstance(decl.init, AddressOf)
        self.assertIsInstance(decl.init.operand, ArraySubscript)
        self.assertIsInstance(ret.value, Dereference)
        self.assertIsInstance(ret.value.operand, VarRef)
        self.assertEqual(ret.value.operand.name, "q")

    def test_extracts_explicit_casts_from_real_clang_ast(self):
        source = """
            typedef struct Header { int type; } Header;
            int parse(void *data) {
                Header *hdr = (Header *)data;
                return hdr->type;
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        parse = funcs["parse"]
        decl = next(stmt for stmt in parse.statements if isinstance(stmt, DeclarationStmt))

        self.assertIsInstance(decl.init, CastExpr)
        self.assertEqual(decl.init.target_type, "Header *")
        self.assertEqual(decl.init.c_type, "Header *")
        self.assertEqual(decl.init.kind, "BitCast")
        self.assertIsInstance(decl.init.expr, VarRef)
        self.assertEqual(decl.init.expr.name, "data")
        self.assertEqual(decl.init.expr.c_type, "void *")

    def test_extracts_array_subscript_assignment_from_real_clang_ast(self):
        source = """
            typedef struct Owner { void *items[4]; int count; } Owner;
            int store(Owner *owner, void *item) {
                owner->items[owner->count++] = item;
                return 0;
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        store = funcs["store"]
        assignment = next(stmt for stmt in store.statements if isinstance(stmt, AssignmentStmt))

        self.assertIsInstance(assignment.target, ArraySubscript)
        self.assertEqual(assignment.target.c_type, "void *")
        self.assertIsInstance(assignment.target.base, FieldAccess)
        self.assertEqual(assignment.target.base.field, "items")
        self.assertIsInstance(assignment.value, VarRef)
        self.assertEqual(assignment.value.name, "item")

    def test_extracts_loop_facts_from_real_clang_ast(self):
        source = """
            int count_until(int limit) {
                int total = 0;
                for (int i = 0; i < limit; i++) {
                    if (i == 3) return total;
                    total += i;
                }
                while (total < 100) {
                    total++;
                }
                return total;
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        loops = [stmt for stmt in funcs["count_until"].statements if isinstance(stmt, LoopStmt)]

        self.assertEqual([loop.kind for loop in loops], ["for", "while"])
        self.assertIsInstance(loops[0].condition, BinaryOp)
        self.assertEqual(loops[0].condition.op, "<")
        self.assertTrue(any(isinstance(stmt, IfStmt) for stmt in loops[0].body))
        self.assertIsInstance(loops[1].condition, BinaryOp)
        self.assertEqual(loops[1].condition.op, "<")

    def test_extracts_function_pointer_field_call_from_real_clang_ast(self):
        source = """
            typedef void (*Handler)(void *ctx);
            typedef struct Runner { Handler handler; void *ctx; } Runner;
            void run(Runner *runner) {
                if (runner->handler) {
                    runner->handler(runner->ctx);
                }
            }
        """
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.c"
            src.write_text(source)

            funcs = parse_translation_unit(src)

        calls = [
            stmt.expr for stmt in funcs["run"].statements
            if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr)
        ]

        self.assertEqual([call.callee for call in calls], ["runner->handler"])


if __name__ == "__main__":
    unittest.main()
