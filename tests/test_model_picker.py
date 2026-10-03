"""Model picker (provider -> model) for /model."""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests  # noqa: F401
import bot


class FakeUser:
    def __init__(self, id, roles=None):
        self.id = id
        if roles is not None:
            self.roles = roles


class FakeResponse:
    def __init__(self):
        self.sent = []      # (content, kwargs)
        self.edited = []    # (content, kwargs)

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def edit_message(self, content=None, **kwargs):
        self.edited.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user_id=12345):
        self.user = FakeUser(user_id)
        self.guild = None
        self.channel = None
        self.channel_id = 555
        self.response = FakeResponse()


ITEMS = ([f"aaa/m{i:02d}" for i in range(30)]
         + [f"zzz/z{i}" for i in range(3)])


class GroupModels(unittest.TestCase):
    def test_groups_and_sorts(self):
        grouped = bot.group_models_by_provider(
            ["zzz/b", "aaa/c", "aaa/a", "zzz/a"])
        self.assertEqual(list(grouped), ["aaa", "zzz"])
        self.assertEqual(grouped["aaa"], ["a", "c"])
        self.assertEqual(grouped["zzz"], ["a", "b"])

    def test_skips_bare_names(self):
        self.assertEqual(bot.group_models_by_provider(["nope", "a/b"]),
                         {"a": ["b"]})


class ModelAutocomplete(unittest.TestCase):
    def _run(self, current):
        with mock.patch.object(bot, "get_models", return_value=list(ITEMS)):
            return asyncio.run(bot.model_autocomplete(FakeInteraction(), current))

    def test_provider_spread_not_single_provider(self):
        # 30 aaa/* models would fill the old flat 25-cap alone; the grouped
        # version must surface zzz/* too.
        out = self._run("")
        values = [c.value for c in out]
        self.assertLessEqual(len(out), 25)
        self.assertTrue(any(v.startswith("zzz/") for v in values),
                        f"no zzz model surfaced: {values}")
        self.assertTrue(any(v.startswith("aaa/") for v in values))

    def test_filter_matches_across_providers(self):
        out = self._run("z0")
        values = [c.value for c in out]
        self.assertIn("zzz/z0", values)

    def test_clear_choice_present(self):
        out = self._run("")
        self.assertIn("__clear__", [c.value for c in out])

    def test_no_items_still_offers_clear(self):
        with mock.patch.object(bot, "get_models", return_value=[]):
            out = asyncio.run(bot.model_autocomplete(FakeInteraction(), ""))
        self.assertEqual([c.value for c in out], ["__clear__"])


class ModelCmd(unittest.TestCase):
    def setUp(self):
        self._overrides = dict(bot.MODEL_OVERRIDES)
        bot.MODEL_OVERRIDES.clear()

    def tearDown(self):
        bot.MODEL_OVERRIDES.clear()
        bot.MODEL_OVERRIDES.update(self._overrides)

    def _run(self, inter, model):
        with mock.patch.object(bot, "get_models", return_value=list(ITEMS)):
            asyncio.run(bot.model_cmd(inter, model))
        return inter

    def test_no_arg_shows_picker(self):
        # The pasted draft had this branch unreachable (dead code after the
        # `if not model` clear); no-arg must pop the picker, not clear.
        inter = self._run(FakeInteraction(), None)
        self.assertEqual(len(inter.response.sent), 1)
        content, kwargs = inter.response.sent[0]
        self.assertIn("Pick a provider", content)
        view = kwargs.get("view")
        self.assertIsInstance(view, bot.ModelPicker)
        providers = [o["value"] for o in view.provider_select.options]
        self.assertEqual(providers, ["aaa", "zzz"])
        self.assertEqual(bot.MODEL_OVERRIDES, {})  # nothing cleared/set

    def test_no_arg_no_models(self):
        inter = FakeInteraction()
        with mock.patch.object(bot, "get_models", return_value=[]):
            asyncio.run(bot.model_cmd(inter, None))
        content, _ = inter.response.sent[0]
        self.assertIn("No models available", content)

    def test_clear_choice(self):
        bot.MODEL_OVERRIDES["dm:12345"] = "aaa/m01"
        inter = self._run(FakeInteraction(), "__clear__")
        self.assertNotIn("dm:12345", bot.MODEL_OVERRIDES)
        content, _ = inter.response.sent[0]
        self.assertIn("cleared", content)

    def test_unknown_model_shows_picker(self):
        inter = self._run(FakeInteraction(), "nope/nothing")
        content, kwargs = inter.response.sent[0]
        self.assertIn("unknown model", content)
        self.assertIsInstance(kwargs.get("view"), bot.ModelPicker)

    def test_known_model_sets_override(self):
        inter = self._run(FakeInteraction(), "zzz/z1")
        self.assertEqual(bot.MODEL_OVERRIDES["dm:12345"], "zzz/z1")
        content, _ = inter.response.sent[0]
        self.assertIn("zzz/z1", content)

    def test_non_dj_denied(self):
        inter = self._run(FakeInteraction(user_id=99999), None)
        content, kwargs = inter.response.sent[0]
        self.assertIn("not permitted", content)
        self.assertTrue(kwargs.get("ephemeral"))


class ModelPickerFlow(unittest.TestCase):
    def setUp(self):
        self._overrides = dict(bot.MODEL_OVERRIDES)
        bot.MODEL_OVERRIDES.clear()

    def tearDown(self):
        bot.MODEL_OVERRIDES.clear()
        bot.MODEL_OVERRIDES.update(self._overrides)

    def test_provider_then_model(self):
        view = bot.ModelPicker(ITEMS, "dm:12345")
        inter = FakeInteraction()

        view.provider_select.values = ["zzz"]
        asyncio.run(view.provider_selected(inter))
        self.assertEqual(len(inter.response.edited), 1)
        models = [o["value"] for o in view.model_select.options]
        self.assertEqual(models, ["zzz/z0", "zzz/z1", "zzz/z2"])

        view.model_select.values = ["zzz/z2"]
        with mock.patch.object(bot, "save_state") as save:
            asyncio.run(view.model_selected(inter))
        save.assert_called_once()
        self.assertEqual(bot.MODEL_OVERRIDES["dm:12345"], "zzz/z2")
        content, kwargs = inter.response.sent[0]
        self.assertIn("zzz/z2", content)
        self.assertTrue(kwargs.get("ephemeral"))

    def test_reselecting_provider_replaces_model_select(self):
        view = bot.ModelPicker(ITEMS, "dm:12345")
        inter = FakeInteraction()
        view.provider_select.values = ["aaa"]
        asyncio.run(view.provider_selected(inter))
        first = view.model_select
        view.provider_select.values = ["zzz"]
        asyncio.run(view.provider_selected(inter))
        self.assertIsNot(view.model_select, first)
        models = [o["value"] for o in view.model_select.options]
        self.assertTrue(all(m.startswith("zzz/") for m in models))


if __name__ == "__main__":
    unittest.main()
