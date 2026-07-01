from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kleva.shaping.ir_parsers import HelperCallRule
from kleva.synth_config import load_helper_call_rules
from kleva.synth_generate import branch_seed_score, generate_yaml_from_header
from kleva.acsl import ACSLBehavior


class SynthIrIntegrationTests(unittest.TestCase):
    def test_branch_seed_prefers_permissive_success_behavior(self):
        bad = ACSLBehavior(
            name="bad",
            assumes=["capacity == 0"],
            ensures=[r"\result == \null"],
        )
        ok = ACSLBehavior(
            name="ok",
            assumes=["capacity > 0"],
            ensures=[r"\result == \null || \valid(\result)"],
        )

        self.assertGreater(branch_seed_score(ok), branch_seed_score(bad))

    def test_no_acsl_null_candidate_uses_ir_null_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "maybe.h"
            source = root / "maybe.c"

            header.write_text(
                """
typedef struct Item {
    int value;
} Item;

int maybe_use(Item *item);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "maybe.h"

int maybe_use(Item *item) {
    if (!item) {
        return -1;
    }
    return item->value;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("# Fallbacks: none", yaml_text)
        self.assertIn("maybe_use_null", yaml_text)
        self.assertIn("int out_ret = maybe_use(NULL);", yaml_text)
        self.assertIn("maybe_use_valid", yaml_text)

    def test_destructor_named_function_does_not_emit_post_free_field_witnesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "box.h"
            source = root / "box.c"

            header.write_text(
                """
typedef struct Box {
    int value;
} Box;

void box_free(Box *box);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "box.h"

static void delegated_destroy(void *ptr) {
    (void)ptr;
}

void box_free(Box *box) {
    if (!box) {
        return;
    }
    delegated_destroy((void *)box);
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("box_free_valid", yaml_text)
        self.assertIn("box_free(box);", yaml_text)
        self.assertIn("int out_call_completed = 1;", yaml_text)
        self.assertNotIn("out_box_value", yaml_text)
        self.assertNotIn("box->value", yaml_text)

    def test_no_yaml_synthesis_gets_public_functions_from_clang_header_ast(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "api.h"
            source = root / "api.c"

            header.write_text(
                """
typedef struct Item {
    int value;
} Item;

/*@
    behavior valid:
        assumes \\valid(item);
        ensures \\result == 0 || \\result == -1;
*/
int use_item(Item *item);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "api.h"

int use_item(Item *item) {
    if (!item) {
        return -1;
    }
    return item->value;
}
""",
                encoding="utf-8",
            )

            with patch("kleva.kernel.program.fallback_parse_header", side_effect=AssertionError("source header fallback used")):
                yaml_text = generate_yaml_from_header(
                    str(header),
                    source_path=str(source),
                    include_dir=str(root),
                )

        self.assertIn("use_item_valid", yaml_text)

    def test_include_static_functions_emits_source_included_internal_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "api.h"
            source = root / "api.c"

            header.write_text("int api(int value);\n", encoding="utf-8")
            source.write_text(
                """
#include "api.h"

static int local_helper(int value) {
    if (value == 0) {
        return -1;
    }
    return value;
}

int api(int value) {
    return local_helper(value);
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                include_static_functions=True,
            )

        self.assertIn("source_included: true", yaml_text)
        self.assertIn(f"header:      {source.resolve()}", yaml_text)
        self.assertIn("api_valid", yaml_text)
        self.assertIn("local_helper_valid", yaml_text)

    def test_valid_char_pointer_contract_gets_string_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "names.h"
            source = root / "names.c"

            header.write_text(
                """
/*@
    behavior valid:
        assumes \\valid(name);
        ensures \\result == 0 || \\result == -1;
*/
int open_name(const char *name);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "names.h"

int open_name(const char *name) {
    if (!name) {
        return -1;
    }
    return name[0] ? 0 : -1;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("open_name_valid", yaml_text)
        self.assertIn("char name_buf[]", yaml_text)
        self.assertIn("kleva", yaml_text)
        self.assertIn("const char * name = name_buf;", yaml_text)
        self.assertIn("int out_ret = open_name(name);", yaml_text)

    def test_valid_read_byte_pointer_contract_gets_byte_buffer_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "bytes.h"
            source = root / "bytes.c"

            header.write_text(
                """
#include <stdint.h>

/*@
    behavior valid:
        assumes \\valid_read(data + (0 .. 7));
        ensures \\result == 0 || \\result == -1;
*/
int parse_bytes(const uint8_t *data);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "bytes.h"

int parse_bytes(const uint8_t *data) {
    if (!data) {
        return -1;
    }
    return data[0] == 0 ? 0 : -1;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("parse_bytes_valid", yaml_text)
        self.assertIn("uint8_t data_buf[8];", yaml_text)
        self.assertIn("const uint8_t * data = data_buf;", yaml_text)
        self.assertIn("int out_ret = parse_bytes(data);", yaml_text)
        self.assertIn("parse_bytes_ir_diversity_data_all_zero", yaml_text)
        self.assertIn("parse_bytes_ir_diversity_data_all_0xff", yaml_text)
        self.assertIn("parse_bytes_ir_diversity_data_first_byte_set", yaml_text)
        self.assertIn("memset(data_buf, 0xFF, sizeof(data_buf));", yaml_text)
        self.assertIn("if (sizeof(data_buf) > 0) data_buf[0] = 1;", yaml_text)
        self.assertIn('diversity: "byte-buffer"', yaml_text)
        self.assertIn('value: "first-byte-set"', yaml_text)

    def test_synthesis_emits_curated_scalar_diversity_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "sizes.h"
            source = root / "sizes.c"

            header.write_text(
                """
#include <stddef.h>

/*@
    behavior valid:
        ensures \\result == 0 || \\result == -1;
*/
int reserve(size_t len);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "sizes.h"

int reserve(size_t len) {
    return len == 0 ? -1 : 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("reserve_ir_diversity_len_zero", yaml_text)
        self.assertIn("int out_ret = reserve(0);", yaml_text)
        self.assertIn("reserve_ir_diversity_len_one", yaml_text)
        self.assertIn("int out_ret = reserve(1);", yaml_text)
        self.assertIn("reserve_ir_diversity_len_two", yaml_text)
        self.assertIn("int out_ret = reserve(2);", yaml_text)
        self.assertIn('diversity: "scalar"', yaml_text)

    def test_valid_writable_byte_pointer_contract_gets_byte_buffer_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "write_bytes.h"
            source = root / "write_bytes.c"

            header.write_text(
                """
#include <stdint.h>

/*@
    behavior valid:
        assumes \\valid(data + (0 .. 7));
        ensures \\result == 0 || \\result == -1;
*/
int fill_bytes(uint8_t *data);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "write_bytes.h"

int fill_bytes(uint8_t *data) {
    if (!data) {
        return -1;
    }
    data[0] = 1;
    return 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("fill_bytes_valid", yaml_text)
        self.assertIn("uint8_t data_buf[8];", yaml_text)
        self.assertIn("uint8_t * data = data_buf;", yaml_text)
        self.assertIn("int out_ret = fill_bytes(data);", yaml_text)

    def test_valid_read_object_path_contract_gets_byte_buffer_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "buffer_obj.h"
            source = root / "buffer_obj.c"

            header.write_text(
                """
#include <stddef.h>
#include <stdint.h>

typedef struct Buffer {
    size_t len;
    uint8_t *data;
} Buffer;

/*@
    behavior valid:
        assumes \\valid(buf);
        assumes buf->len == 8;
        assumes \\valid_read(buf->data + (0 .. buf->len - 1));
        ensures \\result == 0 || \\result == -1;
*/
int parse_buffer(Buffer *buf);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "buffer_obj.h"

int parse_buffer(Buffer *buf) {
    if (!buf || !buf->data) {
        return -1;
    }
    return buf->len == 8 ? 0 : -1;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("parse_buffer_valid", yaml_text)
        self.assertIn("uint8_t buf_data_buffer[64];", yaml_text)
        self.assertIn("if (buf.data == NULL) buf.data = buf_data_buffer;", yaml_text)
        self.assertIn("memset(buf.data, 0, buf.len);", yaml_text)
        self.assertIn("int out_ret = parse_buffer(&buf);", yaml_text)

    def test_no_yaml_synthesis_does_not_scan_source_bodies_on_default_ir_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "machine.h"
            source = root / "machine.c"

            header.write_text(
                """
typedef struct Machine {
    int state;
} Machine;

/*@
    behavior valid:
        assumes \\valid(m);
        ensures \\result == 0 || \\result == -1;
*/
int machine_step(Machine *m);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "machine.h"

int machine_step(Machine *m) {
    if (!m) {
        return -1;
    }
    switch (m->state) {
        case 1:
            return 0;
        default:
            return -1;
    }
}
""",
                encoding="utf-8",
            )

            with patch("kleva.synth_ops._fallback_function_body", side_effect=AssertionError("source body fallback used")):
                yaml_text = generate_yaml_from_header(
                    str(header),
                    source_path=str(source),
                    include_dir=str(root),
                )

        self.assertIn("machine_step_ir_case_state_1", yaml_text)
        self.assertIn("m.state = 1;", yaml_text)

    def test_no_yaml_synthesis_uses_ir_buffer_facts_for_len_data_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "buffer.h"
            source = root / "buffer.c"

            header.write_text(
                """
#include <stddef.h>
#include <stdint.h>

typedef struct Buffer {
    size_t len;
    uint8_t *data;
} Buffer;

/*@
    behavior valid:
        assumes \\valid(buf);
        ensures \\result == 0 || \\result == -1;
*/
int consume(Buffer *buf);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "buffer.h"

int consume(Buffer *buf) {
    if (!buf || buf->len == 0 || !buf->data) {
        return -1;
    }
    return buf->data[0] == 0 ? 0 : -1;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("uint8_t buf_data[64];", yaml_text)
        self.assertIn("if (buf.data == NULL) buf.data = buf_data;", yaml_text)
        self.assertIn("if (buf.len == 0) buf.len = 8;", yaml_text)

    def test_no_yaml_synthesis_uses_ir_void_cast_type_for_void_param_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "dispatch.h"
            source = root / "dispatch.c"

            header.write_text(
                """
typedef struct Context {
    int ready;
} Context;

/*@
    behavior valid:
        assumes ctx != \\null;
        ensures \\result == 0 || \\result == -1;
*/
int dispatch(void *ctx);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "dispatch.h"

int dispatch(void *ctx) {
    Context *typed = (Context *)ctx;
    return typed->ready ? 0 : -1;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("Context ctx_Context;", yaml_text)
        self.assertIn("memset(&ctx_Context, 0, sizeof(ctx_Context));", yaml_text)
        self.assertIn("int out_ret = dispatch(&ctx_Context);", yaml_text)

    def test_no_yaml_synthesis_uses_ir_state_switch_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "machine.h"
            source = root / "machine.c"

            header.write_text(
                """
typedef struct Machine {
    int state;
} Machine;

/*@
    behavior valid:
        assumes \\valid(m);
        ensures \\result == 0 || \\result == -1;
*/
int machine_step(Machine *m);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "machine.h"

int machine_step(Machine *m) {
    if (!m) {
        return -1;
    }
    switch (m->state) {
        case 1:
            return 0;
        case 2:
            return 0;
        default:
            return -1;
    }
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("machine_step_ir_case_state_1", yaml_text)
        self.assertIn("machine_step_ir_case_state_2", yaml_text)
        self.assertIn("machine_step_ir_default_state", yaml_text)
        self.assertIn("m.state = 1;", yaml_text)
        self.assertIn("m.state = 2;", yaml_text)
        self.assertIn("m.state = 0;", yaml_text)
        self.assertNotIn("&m.state", yaml_text)

    def test_synthesis_can_emit_ir_json_for_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "machine.h"
            source = root / "machine.c"
            ir_json = root / "ir.json"

            header.write_text(
                """
typedef struct Machine {
    int state;
} Machine;

/*@
    behavior valid:
        assumes \\valid(m);
        ensures \\result == 0 || \\result == -1;
*/
int machine_step(Machine *m);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "machine.h"

int machine_step(Machine *m) {
    switch (m->state) {
        case 1:
            return 0;
        default:
            return -1;
    }
}
""",
                encoding="utf-8",
            )

            generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                emit_ir_path=str(ir_json),
            )

            data = json.loads(ir_json.read_text(encoding="utf-8"))

        self.assertEqual(data["machine_step"]["kind"], "FunctionIR")
        self.assertEqual(data["machine_step"]["statements"][0]["kind"], "SwitchStmt")
        self.assertEqual(data["machine_step"]["statements"][0]["selector"]["field"], "state")

    def test_synthesis_can_emit_ir_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "machine.h"
            source = root / "machine.c"
            diagnostics_json = root / "ir-diagnostics.json"

            header.write_text(
                """
typedef struct Machine {
    int state;
} Machine;

/*@
    behavior valid:
        assumes \\valid(m);
        ensures \\result == 0 || \\result == -1;
*/
int machine_step(Machine *m);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "machine.h"

int machine_step(Machine *m) {
    return m ? 0 : -1;
}
""",
                encoding="utf-8",
            )

            generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                ir_diagnostics_path=str(diagnostics_json),
            )

            data = json.loads(diagnostics_json.read_text(encoding="utf-8"))

        self.assertEqual(data, [{
            "backend": "clang-json",
            "error": None,
            "source": str(source),
            "status": "ok",
        }])

    def test_ir_diagnostics_records_disabled_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "machine.h"
            source = root / "machine.c"
            diagnostics_json = root / "ir-diagnostics.json"

            header.write_text(
                """
/*@
    behavior valid:
        assumes \true;
        ensures \result == 0;
*/
int machine_step(void);
""",
                encoding="utf-8",
            )
            source.write_text("int machine_step(void) { return 0; }\n", encoding="utf-8")

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                ir_backend="off",
                ir_diagnostics_path=str(diagnostics_json),
            )

            data = json.loads(diagnostics_json.read_text(encoding="utf-8"))

        self.assertEqual(data[0]["backend"], "off")
        self.assertEqual(data[0]["status"], "disabled")
        self.assertIsNone(data[0]["error"])
        self.assertEqual(data[1]["backend"], "source-fallback")
        self.assertEqual(data[1]["status"], "used")
        self.assertIn("IR backend is off", data[1]["error"])
        self.assertIn("candidate_facts:", yaml_text)
        self.assertIn("kind: fallback", yaml_text)
        self.assertIn('function: "machine_step"', yaml_text)
        self.assertIn('scope: "function"', yaml_text)

    def test_ir_diagnostics_records_extraction_failure_without_stopping_synthesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "broken.h"
            source = root / "broken.c"
            diagnostics_json = root / "ir-diagnostics.json"

            header.write_text("int broken_step(void);\n", encoding="utf-8")
            source.write_text("int broken_step(void) { return ;;; }\n", encoding="utf-8")

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                ir_diagnostics_path=str(diagnostics_json),
            )

            data = json.loads(diagnostics_json.read_text(encoding="utf-8"))

        self.assertIn("broken_step", yaml_text)
        self.assertIn("# Fallbacks: used", yaml_text)
        self.assertIn("# Fallback: function/type metadata parsed with source-text fallback", yaml_text)
        self.assertEqual(data[0]["backend"], "clang-json")
        self.assertEqual(data[0]["status"], "failed")
        self.assertIn("CalledProcessError", data[0]["error"])
        self.assertEqual(data[1]["backend"], "source-fallback")
        self.assertEqual(data[1]["status"], "used")
        self.assertIn("source-text fallback", data[1]["error"])

    def test_ir_backend_off_suppresses_ir_state_switch_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "machine.h"
            source = root / "machine.c"

            header.write_text(
                """
typedef struct Machine {
    int state;
} Machine;

/*@
    behavior valid:
        assumes \\valid(m);
        ensures \\result == 0 || \\result == -1;
*/
int machine_step(Machine *m);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "machine.h"

int machine_step(Machine *m) {
    if (!m) {
        return -1;
    }
    switch (m->state) {
        case 1:
            return 0;
        default:
            return -1;
    }
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                ir_backend="off",
            )

        self.assertNotIn("machine_step_ir_case_state_1", yaml_text)
        self.assertNotIn("m.state = 1;", yaml_text)

    def test_no_yaml_synthesis_uses_ir_callee_success_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "worker.h"
            source = root / "worker.c"

            header.write_text(
                """
typedef struct Worker {
    int ready;
} Worker;

/*@
    behavior valid:
        assumes \\valid(w);
        ensures \\result == 0 || \\result == -1;
*/
int run(Worker *w);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "worker.h"

int prepare(Worker *w) {
    if (!w->ready) {
        return -1;
    }
    return 0;
}

int run(Worker *w) {
    if (!w) {
        return -1;
    }
    if (prepare(w) == -1) {
        return -1;
    }
    return 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("run_ir_callee_prepare_equals__1_success", yaml_text)
        self.assertIn("w.ready = 1;", yaml_text)
        self.assertNotIn("out_ir_callee_prepare_equals__1_success_w_ready_nonzero", yaml_text)
        self.assertNotIn("int out_ir_callee_prepare_equals__1_success_w_ready_nonzero = (w.ready != 0);", yaml_text)

    def test_no_yaml_synthesis_gets_helper_signatures_from_clang_decls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "worker.h"
            source = root / "worker.c"

            header.write_text(
                """
typedef struct Worker {
    int ready;
} Worker;

/*@
    behavior valid:
        assumes \\valid(w);
        ensures \\result == 0 || \\result == -1;
*/
int run(Worker *w);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "worker.h"

int prepare(Worker *w) {
    if (!w->ready) {
        return -1;
    }
    return 0;
}

int run(Worker *w) {
    if (prepare(w) == -1) {
        return -1;
    }
    return 0;
}
""",
                encoding="utf-8",
            )

            with patch("kleva.kernel.program.fallback_function_decl_map", side_effect=AssertionError("source decl fallback used")):
                yaml_text = generate_yaml_from_header(
                    str(header),
                    source_path=str(source),
                    include_dir=str(root),
                )

        self.assertIn("run_ir_callee_prepare_equals__1_success", yaml_text)
        self.assertIn("w.ready = 1;", yaml_text)

    def test_no_yaml_synthesis_gets_type_catalog_from_clang_ast(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "callbacks.h"
            source = root / "callbacks.c"

            header.write_text(
                """
typedef void (*RecvFn)(int value, void *ctx);

typedef struct Holder {
    RecvFn recv;
} Holder;

/*@
    behavior valid:
        assumes \\valid(h);
        ensures \\result == 0;
*/
int install(Holder *h);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "callbacks.h"

int install(Holder *h) {
    if (h->recv) {
        h->recv(7, 0);
    }
    return 0;
}
""",
                encoding="utf-8",
            )

            with patch("kleva.kernel.program.fallback_build_type_catalog", side_effect=AssertionError("source type fallback used")):
                yaml_text = generate_yaml_from_header(
                    str(header),
                    source_path=str(source),
                    include_dir=str(root),
                )

        self.assertIn("Holder h;", yaml_text)
        self.assertIn("memset(&h, 0, sizeof(h));", yaml_text)
        self.assertIn("install_ir_callback_h_recv_present", yaml_text)
        self.assertIn("h.recv = kleva_stub_RecvFn;", yaml_text)

    def test_no_yaml_synthesis_uses_scalar_ir_callee_success_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "worker.h"
            source = root / "worker.c"

            header.write_text(
                """
/*@
    behavior valid:
        ensures \\result == 0 || \\result == -1;
*/
int run(int limit);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "worker.h"

int check_size(int size) {
    if (size == 0) {
        return -1;
    }
    return 0;
}

int run(int limit) {
    if (check_size(limit) == -1) {
        return -1;
    }
    return 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("run_ir_callee_check_size_equals__1_success", yaml_text)
        self.assertIn("limit = 1;", yaml_text)
        self.assertNotIn("int out_ir_callee_check_size_equals__1_success_limit_nonzero = (limit != 0);", yaml_text)

    def test_no_yaml_synthesis_uses_ir_parser_boundary_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "parser.h"
            source = root / "parser.c"

            header.write_text(
                """
typedef struct Input {
    int size;
} Input;

/*@
    behavior valid:
        assumes \\valid(input);
        ensures \\result == 0 || \\result == -1;
*/
int parse(Input *input);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "parser.h"

int parse(Input *input) {
    if (!input) {
        return -1;
    }
    if (input->size < 8) {
        return -1;
    }
    return 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("parse_ir_min_guard_1_input_size_lt_8_too_low", yaml_text)
        self.assertIn("parse_ir_min_guard_1_input_size_lt_8_boundary", yaml_text)
        self.assertIn("parse_ir_min_guard_1_input_size_lt_8_valid_high", yaml_text)
        self.assertIn("input.size = 7;", yaml_text)
        self.assertIn("input.size = 8;", yaml_text)
        self.assertIn("input.size = 9;", yaml_text)

    def test_no_yaml_synthesis_uses_ir_parser_equality_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "parser.h"
            source = root / "parser.c"

            header.write_text(
                """
typedef struct Input {
    int tag;
} Input;

/*@
    behavior valid:
        assumes \\valid(input);
        ensures \\result == 0 || \\result == -1;
*/
int parse(Input *input);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "parser.h"

int parse(Input *input) {
    if (!input) {
        return -1;
    }
    if (input->tag != 7) {
        return -1;
    }
    return 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("parse_ir_required_value_1_input_tag_ne_7_required", yaml_text)
        self.assertIn("parse_ir_required_value_1_input_tag_ne_7_other", yaml_text)
        self.assertIn("input.tag = 7;", yaml_text)
        self.assertIn("input.tag = 8;", yaml_text)

    def test_no_yaml_synthesis_uses_ir_parser_call_guard_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "parser.h"
            source = root / "parser.c"

            header.write_text(
                """
typedef struct Input {
    int value;
} Input;

/*@
    behavior valid:
        assumes \\valid(input);
        ensures \\result == 0 || \\result == -1;
*/
int parse(Input *input);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "parser.h"

int verify(Input *input) {
    return input->value == 0;
}

int parse(Input *input) {
    if (!input) {
        return -1;
    }
    if (verify(input) != 0) {
        return -1;
    }
    return 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
            )

        self.assertIn("parse_ir_call_guard_1_verify_ne_0_success", yaml_text)
        self.assertIn("parse_ir_call_guard_1_verify_ne_0_failure", yaml_text)
        self.assertIn("input.value = 1;", yaml_text)
        self.assertIn("input.value = 0;", yaml_text)

    def test_no_yaml_synthesis_applies_helper_call_repair_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "parser.h"
            source = root / "parser.c"

            header.write_text(
                """
typedef struct Input {
    int value;
} Input;

/*@
    behavior valid:
        assumes \\valid(input);
        ensures \\result == 0 || \\result == -1;
*/
int parse(Input *input);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "parser.h"

int verify(Input *input) {
    return input->value == 0;
}

int parse(Input *input) {
    if (!input) {
        return -1;
    }
    if (verify(input) != 0) {
        return -1;
    }
    return 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                helper_call_rules=(
                    HelperCallRule(
                        "verify",
                        success_setup=("{arg0}->value = 1;",),
                        failure_setup=("{arg0}->value = 0;",),
                    ),
                ),
            )

        self.assertIn("parse_ir_call_guard_1_verify_ne_0_success", yaml_text)
        self.assertIn("input.value = 1;", yaml_text)
        self.assertIn("parse_ir_call_guard_1_verify_ne_0_failure", yaml_text)
        self.assertIn("input.value = 0;", yaml_text)

    def test_no_yaml_synthesis_applies_helper_call_repair_rules_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "parser.h"
            source = root / "parser.c"
            rules = root / "rules.yaml"

            header.write_text(
                """
typedef struct Input {
    int value;
} Input;

/*@
    behavior valid:
        assumes \\valid(input);
        ensures \\result == 0 || \\result == -1;
*/
int parse(Input *input);
""",
                encoding="utf-8",
            )
            source.write_text(
                """
#include "parser.h"

int verify(Input *input) {
    return input->value == 0;
}

int parse(Input *input) {
    if (!input) {
        return -1;
    }
    if (verify(input) != 0) {
        return -1;
    }
    return 0;
}
""",
                encoding="utf-8",
            )
            rules.write_text(
                """
helper_call_rules:
  - callee: verify
    success_setup:
      - "{arg0}->value = 1;"
    failure_setup:
      - "{arg0}->value = 0;"
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                helper_call_rules=load_helper_call_rules([str(rules)]),
            )

        self.assertIn("input.value = 1;", yaml_text)
        self.assertIn("input.value = 0;", yaml_text)


if __name__ == "__main__":
    unittest.main()
