import unittest

from kleva.ast.model import CFunction, CParam
from kleva.shaping.source_aliases import (
    cast_alias_backing_setup,
    cast_aliases,
    decoded_field_aliases,
    derived_local_aliases,
    direct_field_aliases,
    expand_alias_expr,
    good_path_setup_from_source,
    setup_local_value,
    void_param_cast_types,
)


def _append_unique(out, line, seen):
    if line not in seen:
        out.append(line)
        seen.add(line)


class SourceAliasShapingTests(unittest.TestCase):
    def test_cast_aliases_and_void_cast_types_are_generic(self):
        body = """
        State *state = (State *)ctx;
        Header *hdr = (Header *)buf->data;
        Other *ignored = (Other *)external;
        """
        params = {
            "ctx": CParam("ctx", "void *", "void", True, False, False, 0),
            "buf": CParam("buf", "Buffer *", "Buffer", True, False, False, 0),
        }
        func = CFunction(
            name="receive",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=list(params.values()),
        )

        self.assertEqual(
            cast_aliases(body, params),
            {"state": ("State", "ctx"), "hdr": ("Header", "buf->data")},
        )
        self.assertEqual(
            void_param_cast_types(body, func, lambda p: p.raw_type == "void *"),
            {"ctx": "State"},
        )

    def test_decoded_direct_and_derived_aliases_shape_original_fields(self):
        body = """
        Header *hdr = (Header *)buf->data;
        uint16_t port = ns_ntohs(hdr->port);
        uint8_t flags = hdr->flags;
        uint8_t syn = flags & SYN_FLAG;
        uint8_t copied = syn;
        uint8_t payload_len = pkt->len - HEADER_LEN;
        if (port == 80) return -1;
        if (copied == SYN_FLAG) return 0;
        if (payload_len == 3) return 0;
        """
        aliases = {"hdr": ("Header", "buf->data")}
        decoded = decoded_field_aliases(body)
        direct = direct_field_aliases(body)
        derived = derived_local_aliases(body)

        self.assertEqual(decoded["port"], ("ns_ntohs", "hdr", "port"))
        self.assertEqual(direct["flags"], ("hdr", "flags"))
        self.assertEqual(derived["copied"].kind, "and")
        self.assertEqual(derived["payload_len"].kind, "field_sub")

        self.assertEqual(
            setup_local_value("port", "80", aliases, decoded, direct, derived),
            ["((Header *)buf->data)->port = ns_htons(80);"],
        )
        self.assertEqual(
            setup_local_value("payload_len", "3", aliases, decoded, direct, derived),
            ["pkt->len = (3) + HEADER_LEN;"],
        )

    def test_good_path_setup_and_backing_setup_emit_generic_fixture_lines(self):
        body = """
        Header *hdr = (Header *)buf->data;
        if (hdr->type != TYPE_OK) return -1;
        uint16_t port = ns_ntohs(hdr->port);
        if (port == 7) return 0;
        """
        aliases = {"hdr": ("Header", "buf->data")}
        lines = good_path_setup_from_source(
            body,
            aliases,
            decoded_field_aliases(body),
            direct_field_aliases(body),
            derived_local_aliases(body),
            _append_unique,
        )

        self.assertIn("((Header *)buf->data)->type = TYPE_OK;", lines)
        self.assertIn("((Header *)buf->data)->port = ns_htons(7);", lines)
        self.assertEqual(
            expand_alias_expr("hdr->type", aliases),
            "((Header *)buf->data)->type",
        )
        self.assertEqual(
            cast_alias_backing_setup(
                "hdr",
                "Header",
                "buf->data",
                {"buf": object()},
                lambda name: name,
            ),
            [
                "Header kleva_hdr_data_storage;",
                "memset(&kleva_hdr_data_storage, 0, sizeof(kleva_hdr_data_storage));",
                "buf->data = &kleva_hdr_data_storage;",
            ],
        )


if __name__ == "__main__":
    unittest.main()
