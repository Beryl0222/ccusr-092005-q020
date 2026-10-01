"""测试夹具：每个用例在临时目录里得到一份从种子重放的服务。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broth.repository import Repository
from broth.seed import seed_database
from broth.service import BrothService

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "fixtures" / "seed.json"


class BrothTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "broth.json"
        self.repo: Repository = seed_database(SEED, self.db_path)
        self.svc = BrothService(self.repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- 常用批次（与 fixtures/seed.json 对应） --
    @property
    def root(self) -> str:
        return "broth-lineage-main"

    @property
    def merge_broth(self) -> str:
        return "broth-merge-0330"

    @property
    def bad_product(self) -> str:
        return "prod-s3-0927"
