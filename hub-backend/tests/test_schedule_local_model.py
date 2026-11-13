"""Local model references are restricted to the owner desktop's Pi worker."""
import unittest

from app.services import schedule_execution as subject


class ScheduleLocalModelTests(unittest.IsolatedAsyncioTestCase):
    def test_only_opaque_local_references_are_device_local(self):
        self.assertTrue(subject.is_device_local_model("local:model-" + "a" * 32))
        self.assertFalse(subject.is_device_local_model("local:ollama:qwen3"))
        self.assertFalse(subject.is_device_local_model("image::ollama::local:model-" + "a" * 32))

    async def test_owner_pi_can_validate_without_disclosing_catalog_or_key(self):
        ref = "local:model-" + "b" * 32
        result = await subject._schedule_model_readiness(None, None, {}, "local:pi", ref)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], ref)
        self.assertIn("本机 Pi", result["detail"])

    async def test_remote_or_non_pi_routes_cannot_use_device_local_reference(self):
        ref = "local:model-" + "c" * 32
        for kind in ("local:codex", "runner:desktop:pi"):
            result = await subject._schedule_model_readiness(None, None, {}, kind, ref)
            self.assertFalse(result["ok"])
            self.assertIn("本机 Pi", result["detail"])


if __name__ == "__main__":
    unittest.main()
