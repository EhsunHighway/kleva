import unittest

from kleva.ast.model import CParam, CTypeCatalog
from kleva.fixtures.buffers import append_len_data_shape, needs_len_data_shape, struct_has_fields


def _param(name, raw_type, base_type, is_pointer=False):
    return CParam(name, raw_type, base_type, is_pointer, False, False, 0)


class BufferFixtureTests(unittest.TestCase):
    def test_struct_has_fields_checks_complete_catalog_fields_generically(self):
        catalog = CTypeCatalog(
            struct_fields={
                "Buffer": {
                    "len":  _param("len", "size_t", "size_t"),
                    "data": _param("data", "uint8_t *", "uint8_t", True),
                }
            }
        )

        self.assertTrue(struct_has_fields(catalog, "Buffer", {"len", "data"}))
        self.assertFalse(struct_has_fields(catalog, "Buffer", {"len", "capacity"}))
        self.assertFalse(struct_has_fields(None, "Buffer", {"len"}))

    def test_needs_len_data_shape_detects_len_read_and_buffer_helper_calls(self):
        catalog = CTypeCatalog(
            struct_fields={
                "Buffer": {
                    "len":  _param("len", "size_t", "size_t"),
                    "data": _param("data", "uint8_t *", "uint8_t", True),
                },
                "Plain": {
                    "len": _param("len", "size_t", "size_t"),
                },
            }
        )
        buffer_param = _param("buf", "Buffer *", "Buffer", True)
        plain_param = _param("plain", "Plain *", "Plain", True)

        self.assertTrue(needs_len_data_shape(
            "handle",
            "buf",
            "source",
            catalog,
            buffer_param,
            lambda _source, _func: "if (buf->len > 0) process(buf->data);",
        ))
        self.assertTrue(needs_len_data_shape(
            "handle",
            "buf",
            "source",
            catalog,
            buffer_param,
            lambda _source, _func: "send_bytes(buf);",
        ))
        self.assertFalse(needs_len_data_shape(
            "handle",
            "plain",
            "source",
            catalog,
            plain_param,
            lambda _source, _func: "if (plain->len > 0) return 1;",
        ))

    def test_append_len_data_shape_initializes_named_buffer_arguments_only(self):
        lines = []
        append_len_data_shape(lines, "buf")
        append_len_data_shape(lines, "NULL")
        append_len_data_shape(lines, "&buf")

        self.assertEqual(lines, [
            "uint8_t buf_data[64];",
            "memset(buf_data, 0, sizeof(buf_data));",
            "if (buf->data == NULL) buf->data = buf_data;",
            "if (buf->len == 0) buf->len = 8;",
            "memset(buf->data, 0, buf->len);",
            "uint8_t buf_data[64];",
            "memset(buf_data, 0, sizeof(buf_data));",
            "if (buf.data == NULL) buf.data = buf_data;",
            "if (buf.len == 0) buf.len = 8;",
            "memset(buf.data, 0, buf.len);",
        ])


if __name__ == "__main__":
    unittest.main()
