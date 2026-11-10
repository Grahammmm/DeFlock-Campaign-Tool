"""Recursion-exhausting private original locators refuse through ModelInputError."""
from copy import deepcopy
import json
import sys
import traceback
import unittest
from unittest import mock

from campaign_tool.digest import model_input
from campaign_tool.digest.build import restore_locators
from campaign_tool.digest.model import ModelError, build_user_message
from campaign_tool.digest.model_input import ModelInputError, ModelSources

FIXED = "model input or locator references failed validation"
SENTINEL = "synthetic-private-sentinel"


def deep_locator():
    # Python 3.11's C JSON encoder spends the interpreter recursion budget on
    # every container (two per level here), so the fixture must stay under
    # limit/2 to be built at all, while deepcopy (four Python frames per
    # level) must still exceed the limit: 0.35 satisfies both.
    node = {"leaf": SENTINEL}
    for _ in range(int(sys.getrecursionlimit() * 0.35)):
        node = {"k": [node]}
    return json.loads(json.dumps(node))


def unit(locator, text="plain text"):
    return {"locator": locator, "text": text}


def formatted(exc):
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class LocalLocatorRecursionTests(unittest.TestCase):
    def test_deep_json_locator_refuses_cleanly(self):
        loc = deep_locator()
        with self.assertRaises(RecursionError):  # fixture really reaches the seam
            deepcopy(loc)
        with self.assertRaises(ModelInputError) as ctx:
            ModelSources([unit({"p": 1}), unit(loc)])
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertNotIn(SENTINEL, formatted(ctx.exception))
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_encoder_fault_is_value_free_refusal(self):
        with mock.patch.object(model_input.json, "dumps", side_effect=RecursionError(SENTINEL)):
            with self.assertRaises(ModelInputError) as ctx:
                ModelSources([unit({"a": 1})])
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertNotIn(SENTINEL, formatted(ctx.exception))

    def test_snapshot_fault_is_value_free_refusal_and_instance_recovers(self):
        with mock.patch.object(model_input, "deepcopy", side_effect=RecursionError(SENTINEL)):
            with self.assertRaises(ModelInputError) as ctx:
                ModelSources([unit({"a": 1})])
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertNotIn(SENTINEL, formatted(ctx.exception))
        self.assertEqual(ModelSources([unit({"a": 1})])._units[0]["locator"], {"a": 1})

    def test_cyclic_and_nonjson_locators_still_refused(self):
        cyc = {}
        cyc["self"] = cyc
        for bad in (cyc, {"a": object()}, {"a": float("nan")}):
            with self.assertRaises(ModelInputError) as ctx:
                ModelSources([unit(bad)])
            self.assertEqual(str(ctx.exception), FIXED)

    def test_wrappers_translate_to_existing_model_errors(self):
        units = [unit(deep_locator())]
        with self.assertRaises(ModelError) as ctx:
            build_user_message(units, [], [])
        self.assertEqual(str(ctx.exception), FIXED)
        self.assertNotIn(SENTINEL, formatted(ctx.exception))
        with self.assertRaises(ModelError) as ctx:
            restore_locators({"scope": "x"}, units)
        self.assertEqual(str(ctx.exception), "model locator references failed validation")
        self.assertNotIn(SENTINEL, formatted(ctx.exception))

    def test_supported_nested_metadata_is_exact_detached_and_private(self):
        loc = {"page": 1, "meta": {"private": [1, {"x": SENTINEL}]}}
        original = deepcopy(loc)
        src = ModelSources([unit(loc), unit(loc)])
        loc["meta"]["private"].append("mutated")
        self.assertEqual(src._units[0]["locator"], original)
        self.assertEqual(src._units[1]["locator"], original)
        self.assertNotIn(SENTINEL, json.dumps(src.source_payload()))


if __name__ == "__main__":
    unittest.main()
