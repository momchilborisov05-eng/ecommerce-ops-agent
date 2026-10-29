"""Shared test setup: a seeded demo runtime in a temporary folder. No network, no API key."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.pop("ANTHROPIC_API_KEY", None)   # tests never reach the real API

from opsagent.config import Settings  # noqa: E402
from opsagent.runtime import Runtime  # noqa: E402


class RuntimeTestCase(unittest.TestCase):
    """Each test gets a fresh, seeded database."""

    seed = True

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="opsagent-test-"))
        self.settings = Settings.from_env(mode="demo", db_path=self.tmp / "test.db")
        self.rt = self.make_runtime()
        if self.seed:
            self.rt.seed()

    def make_runtime(self, **kwargs) -> Runtime:
        return Runtime.build(self.settings, **kwargs)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
