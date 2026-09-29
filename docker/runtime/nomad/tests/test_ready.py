# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FE_HEADER = "Name\tHost\tEditLogPort\tRole\tIsMaster\tJoin\tAlive\n"
BE_HEADER = "BackendId\tAlive\tHost\tHeartbeatPort\n"


class ReadyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "my.cnf").write_text('[client]\npassword="test-only"\n')
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        # Each host answers from tables/<host>/<statement>; a missing file is
        # an unreachable host or failed authentication.
        mysql = bin_dir / "mysql"
        mysql.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "host = sys.argv[sys.argv.index('-h') + 1]\n"
            "table = Path(os.environ['TEST_BASE']) / 'tables' / host / sys.argv[-1]\n"
            "if not table.exists():\n"
            "    sys.exit(1)\n"
            "print(table.read_text(), end='')\n"
        )
        mysql.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            TEST_BASE=str(self.base),
            DORIS_MY_CNF=str(self.base / "my.cnf"),
        )
        self.env.pop("FE_MASTER_IP", None)

    def answer(self, host, statement, rows):
        path = self.base / "tables" / host / statement
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rows)

    def ready(self, *args):
        return subprocess.run(
            ["bash", str(ROOT / "scripts/ready.sh"), *args], env=self.env,
            text=True, capture_output=True, timeout=10,
        )

    def test_joined_fe_with_master_is_ready(self):
        self.answer("10.0.0.3", "SHOW FRONTENDS", FE_HEADER
                    + "fe1\t10.0.0.1\t9010\tFOLLOWER\ttrue\ttrue\ttrue\n"
                    + "fe3\t10.0.0.3\t9010\tFOLLOWER\tfalse\ttrue\ttrue\n")
        result = self.ready("fe", "10.0.0.3")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_fe_not_joined_is_critical(self):
        self.answer("10.0.0.3", "SHOW FRONTENDS", FE_HEADER
                    + "fe1\t10.0.0.1\t9010\tFOLLOWER\ttrue\ttrue\ttrue\n"
                    + "fe3\t10.0.0.3\t9010\tFOLLOWER\tfalse\tfalse\ttrue\n")
        result = self.ready("fe", "10.0.0.3")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not joined", result.stdout)

    def test_fe_without_alive_master_is_critical(self):
        self.answer("10.0.0.3", "SHOW FRONTENDS", FE_HEADER
                    + "fe1\t10.0.0.1\t9010\tFOLLOWER\ttrue\ttrue\tfalse\n"
                    + "fe3\t10.0.0.3\t9010\tFOLLOWER\tfalse\ttrue\ttrue\n")
        result = self.ready("fe", "10.0.0.3")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no alive master", result.stdout)

    def test_fe_authentication_failure_is_critical(self):
        result = self.ready("fe", "10.0.0.3")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("test-only", result.stdout + result.stderr)

    def test_alive_be_is_ready_via_prestart_master(self):
        self.env["FE_MASTER_IP"] = "10.0.0.2"
        self.answer("10.0.0.2", "SHOW BACKENDS", BE_HEADER + "10001\ttrue\t10.0.0.5\t9050\n")
        result = self.ready("be", "10.0.0.5", "10.0.0.1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("FE 10.0.0.2", result.stdout)

    def test_be_falls_back_to_seeds(self):
        self.env["FE_MASTER_IP"] = "10.0.0.2"
        self.answer("10.0.0.1", "SHOW BACKENDS", BE_HEADER + "10001\ttrue\t10.0.0.5\t9050\n")
        result = self.ready("be", "10.0.0.5", "10.0.0.1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("FE 10.0.0.1", result.stdout)

    def test_dead_or_unregistered_be_is_critical(self):
        for rows in (BE_HEADER + "10001\tfalse\t10.0.0.5\t9050\n",
                     BE_HEADER + "10001\ttrue\t10.0.0.6\t9050\n"):
            with self.subTest(rows=rows):
                self.answer("10.0.0.1", "SHOW BACKENDS", rows)
                result = self.ready("be", "10.0.0.5", "10.0.0.1")
                self.assertEqual(result.returncode, 2)

    def test_be_without_reachable_fe_is_critical(self):
        result = self.ready("be", "10.0.0.5", "10.0.0.1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No FE", result.stdout)


if __name__ == "__main__":
    unittest.main()
