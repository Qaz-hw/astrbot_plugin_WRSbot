"""Regression tests for safe weekly-report file selection.

Run from the plugin root:
    python3 -m unittest discover -s tests -v

The Drive service accepts an LLM fallback answer only when it is an exact
filename returned by Feishu.  These tests keep that safety boundary in place:
an LLM must never cause the bot to use an invented file name.
"""

from __future__ import annotations

import logging
import random
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _install_runtime_stubs() -> None:
    """Allow these unit tests to run outside the AstrBot runtime."""
    try:
        import astrbot.api  # noqa: F401
    except ModuleNotFoundError:
        astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
        astrbot_api = types.ModuleType("astrbot.api")
        astrbot_api.logger = logging.getLogger("test.drive")
        sys.modules["astrbot.api"] = astrbot_api
        astrbot.api = astrbot_api

    try:
        import lark_oapi.api.drive.v1  # noqa: F401
    except ModuleNotFoundError:
        lark_oapi = sys.modules.setdefault("lark_oapi", types.ModuleType("lark_oapi"))
        lark_api = sys.modules.setdefault("lark_oapi.api", types.ModuleType("lark_oapi.api"))
        lark_drive = sys.modules.setdefault(
            "lark_oapi.api.drive", types.ModuleType("lark_oapi.api.drive")
        )
        lark_v1 = types.ModuleType("lark_oapi.api.drive.v1")

        class _UnusedListFileRequest:
            """The tests override list_folder_files, so this builder is never used."""

        lark_v1.ListFileRequest = _UnusedListFileRequest
        sys.modules["lark_oapi.api.drive.v1"] = lark_v1
        lark_oapi.api = lark_api
        lark_api.drive = lark_drive
        lark_drive.v1 = lark_v1


_install_runtime_stubs()

from services.drive import DriveService  # noqa: E402


_REAL_FOLDER_FILES = [
    {
        "name": "L3-RD-算法一组（应用）周报-2026.07.27~2026.07.31",
        "type": "docx",
        "token": "doc_last_week",
    },
    {"name": "", "type": "docx", "token": "untitled_doc"},
    {
        "name": "L3-RD-算法一组（应用）周报-2026.07.20~2026.07.24",
        "type": "docx",
        "token": "doc_two_weeks_ago",
    },
]

# Answers a model may produce even though they are not a file returned by
# Feishu. Every one of these must be rejected.
_NONEXISTENT_OR_NONEXACT_ANSWERS = [
    # The exact hallucination that appeared in production.
    "L3-RD-算法一组（应用）周报-2026.08.03~2026.08.07",
    # Plausible dates inferred from the weekly sequence.
    "L3-RD-算法一组（应用）周报-2026.08.10~2026.08.14",
    "L3-RD-算法一组（应用）周报-2026.07.28~2026.08.01",
    # Near-matches: date punctuation, separators, spacing, and title changes.
    "L3-RD-算法一组（应用）周报-2026/07/27~2026/07/31",
    "L3-RD-算法一组（应用）周报-2026.07.27-2026.07.31",
    "L3-RD-算法一组（应用）周报-2026.07.27 ～ 2026.07.31",
    "L3-RD-算法一组（应用）周报-2026.07.27~2026.07.31.docx",
    "L3-RD-算法一组(应用)周报-2026.07.27~2026.07.31",
    "L3-RD-算法一组（应用）周报 - 2026.07.27~2026.07.31",
    # Model chatter instead of the requested bare name.
    "本周文件是：L3-RD-算法一组（应用）周报-2026.07.27~2026.07.31",
    "- L3-RD-算法一组（应用）周报-2026.07.27~2026.07.31",
    "```\nL3-RD-算法一组（应用）周报-2026.07.27~2026.07.31\n```",
    "none",
    "未找到本周周报",
    "",
    "random-file.docx",
]


class _FakeDriveService(DriveService):
    """Drive service with a fixed Feishu folder listing."""

    def __init__(self, files: list[dict]):
        super().__init__(lark_api=None)
        self._files = files

    async def list_folder_files(self, folder_token: str) -> list[dict]:
        self.assert_folder_token(folder_token)
        return list(self._files)

    @staticmethod
    def assert_folder_token(folder_token: str) -> None:
        if folder_token != "folder_token":
            raise AssertionError(f"unexpected folder token: {folder_token}")


class DriveFileSelectionTests(unittest.IsolatedAsyncioTestCase):
    """The LLM cannot make a non-existent weekly-report file selectable."""

    def setUp(self) -> None:
        # The service intentionally logs every rejected response. Silence those
        # expected warnings so a large regression matrix stays readable.
        self._logger_patcher = patch("services.drive.logger")
        self._logger_patcher.start()
        self.addCleanup(self._logger_patcher.stop)

    async def _find_after_forcing_llm_fallback(self, llm_answer: str | None) -> dict | None:
        service = _FakeDriveService(_REAL_FOLDER_FILES)

        async def llm_fn(names: list[str]) -> str | None:
            self.assertEqual(names, [file["name"] for file in _REAL_FOLDER_FILES])
            return llm_answer

        # Keep this test independent of the calendar date: it exercises the
        # branch entered only after deterministic week matching finds nothing.
        with patch.object(DriveService, "match_this_week", return_value=None):
            return await service.find_this_week_file("folder_token", llm_fn=llm_fn)

    async def test_rejects_the_hallucinated_filename_from_the_production_log(self):
        selected = await self._find_after_forcing_llm_fallback(
            "L3-RD-算法一组（应用）周报-2026.08.03~2026.08.07"
        )

        self.assertIsNone(selected)

    async def test_rejects_an_altered_version_of_a_real_filename(self):
        selected = await self._find_after_forcing_llm_fallback(
            "L3-RD-算法一组（应用）周报-2026/07/27~2026/07/31"
        )

        self.assertIsNone(selected)

    async def test_rejects_a_matrix_of_plausible_hallucinations_and_model_chatter(self):
        for answer in _NONEXISTENT_OR_NONEXACT_ANSWERS:
            with self.subTest(answer=answer):
                selected = await self._find_after_forcing_llm_fallback(answer)
                self.assertIsNone(selected)

    async def test_rejects_many_generated_nonexistent_filenames(self):
        """Exercise the invariant against deterministic fuzzed LLM output."""
        rng = random.Random(20260806)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-_~"
        candidates = {
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 120)))
            for _ in range(128)
        }
        real_names = {file["name"] for file in _REAL_FOLDER_FILES}

        for answer in candidates - real_names:
            with self.subTest(answer=answer):
                selected = await self._find_after_forcing_llm_fallback(answer)
                self.assertIsNone(selected)

    async def test_accepts_only_an_exact_filename_returned_by_feishu(self):
        selected = await self._find_after_forcing_llm_fallback(
            "L3-RD-算法一组（应用）周报-2026.07.27~2026.07.31"
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["token"], "doc_last_week")

    async def test_none_response_does_not_select_a_file(self):
        selected = await self._find_after_forcing_llm_fallback(None)

        self.assertIsNone(selected)

    async def test_llm_failure_does_not_select_a_file(self):
        service = _FakeDriveService(_REAL_FOLDER_FILES)

        async def failing_llm(_: list[str]) -> str | None:
            raise RuntimeError("provider timed out")

        with patch.object(DriveService, "match_this_week", return_value=None):
            selected = await service.find_this_week_file("folder_token", llm_fn=failing_llm)

        self.assertIsNone(selected)

    async def test_empty_folder_never_calls_the_llm_or_selects_a_file(self):
        service = _FakeDriveService([])
        llm_called = False

        async def llm_fn(_: list[str]) -> str | None:
            nonlocal llm_called
            llm_called = True
            return "L3-RD-算法一组（应用）周报-2026.08.03~2026.08.07"

        selected = await service.find_this_week_file("folder_token", llm_fn=llm_fn)

        self.assertIsNone(selected)
        self.assertFalse(llm_called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
