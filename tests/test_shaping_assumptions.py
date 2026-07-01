import unittest

from kleva.ast.model import CParam
from kleva.shaping.assumptions import assumption_setup_lines, setup_for_quantified_arrays


def _param(name, raw_type, base_type, is_pointer=True):
    return CParam(name, raw_type, base_type, is_pointer, False, False, 0)


class AssumptionShapingTests(unittest.TestCase):
    def test_quantified_exists_and_forall_arrays(self):
        exists = r"\exists integer i; 0 <= i < n && table->items[i].valid == 1"
        forall = r"\forall integer i; 0 <= i < n ==> table->items[i].valid == 0"

        self.assertEqual(
            setup_for_quantified_arrays(exists, None, None),
            ["table->items[0].valid = 1;"],
        )
        self.assertEqual(
            setup_for_quantified_arrays(forall, None, None),
            ["for (int kleva_i = 0; kleva_i < n; kleva_i++) table->items[kleva_i].valid = 0;"],
        )

    def test_shapes_simple_field_relations_without_buffer_fixture_allocation(self):
        params = {
            "pkt": _param("pkt", "Packet *", "Packet"),
            "iface": _param("iface", "Interface *", "Interface"),
        }
        assumes = [
            "pkt->len >= 8 && pkt->data >= pkt->head + 20",
            r"\valid_read(pkt->data + (0 .. pkt->len - 1))",
            "iface->up != 0",
        ]

        setup = assumption_setup_lines(assumes, params, shaping_features=set())

        self.assertIn("pkt->len = 8;", setup)
        self.assertIn("pkt->data = pkt->head + 20;", setup)
        self.assertIn("iface->up = ((0) + 1);", setup)
        self.assertNotIn("pkt_read_data", "\n".join(setup))
        self.assertNotIn("memset(pkt->data, 0, pkt->len);", setup)

    def test_shapes_correlation_and_casted_data_field(self):
        params = {
            "packet": _param("packet", "Packet *", "Packet"),
            "src": _param("src", "Device *", "Device"),
        }
        assumes = [
            "src == packet->owner",
            "((Header *)packet->data)->type == 8",
        ]

        setup = assumption_setup_lines(assumes, params, param_args={"src": "source_dev"}, shaping_features=set())

        self.assertIn("source_dev = packet->owner;", setup)
        self.assertIn("((Header *)packet->data)->type = 8;", setup)

    def test_shapes_nested_field_relations(self):
        params = {
            "obj": _param("obj", "Object *", "Object"),
        }

        setup = assumption_setup_lines(
            ["obj->state.used >= obj->state.limit"],
            params,
            shaping_features=set(),
        )

        self.assertIn("obj->state.used = obj->state.limit;", setup)

    def test_shapes_scalar_to_field_relations(self):
        params = {
            "pkt": _param("pkt", "Packet *", "Packet"),
        }

        setup = assumption_setup_lines(
            ["header_len <= pkt->len"],
            params,
            param_args={"header_len": "1"},
            shaping_features=set(),
        )

        self.assertIn("pkt->len = 1;", setup)

    def test_shapes_pointer_distance_relations(self):
        params = {
            "p": _param("p", "Packet *", "Packet"),
        }

        low = assumption_setup_lines(
            ["(size_t)(p->data - p->head) < header_len"],
            params,
            param_args={"header_len": "1"},
            shaping_features=set(),
        )
        high = assumption_setup_lines(
            ["(size_t)(p->data - p->head) >= header_len"],
            params,
            param_args={"header_len": "1"},
            shaping_features=set(),
        )

        self.assertIn("p->data = p->head;", low)
        self.assertIn("p->data = p->head + 1;", high)


if __name__ == "__main__":
    unittest.main()
