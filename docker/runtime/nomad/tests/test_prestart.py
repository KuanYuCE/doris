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
# Deliberately reorder the relevant columns: discovery must use headers.
FRONTENDS = (
    "Name\tIsMaster\tHost\tEditLogPort\tHttpPort\tQueryPort\tRpcPort\tRole\t"
    "ClusterId\tJoin\tAlive\tReplayedJournalId\tLastStartTime\tLastHeartbeat\t"
    "IsHelper\tErrMsg\tVersion\tCurrentConnected\n"
    "fe2\ttrue\t10.0.0.2\t9010\t8030\t9030\t9020\tFOLLOWER\t123\ttrue\ttrue\t"
    "100\t2026-09-25\t2026-09-25\ttrue\t\t4.1.4\tYes\n"
)


class PrestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for kind in ("fe", "be"):
            conf = self.base / "image" / kind / "conf"
            conf.mkdir(parents=True)
            (conf / f"{kind}.conf").write_text("# image default\ncustom_option = preserved\n")
        self.meta = self.base / "image/fe/doris-meta"
        self.meta.mkdir()
        (self.base / "image/be/storage").mkdir()
        self.data = self.base / "alloc"
        self.data.mkdir()
        (self.base / "my.cnf").write_text('[client]\npassword="test-only"\n')
        (self.base / "hash").write_text("*" + "A" * 40)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        # Only the external database is replaced. Run the real shell program,
        # configuration preparation and BASH_ENV consumption.
        mysql = self.bin / "mysql"
        mysql.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "base = Path(os.environ['TEST_BASE'])\n"
            "host = sys.argv[sys.argv.index('-h') + 1]\n"
            "sql = sys.argv[-1]\n"
            "if host not in os.environ.get('TEST_REACHABLE_HOSTS', '10.0.0.2').split() or os.environ.get('TEST_OFFLINE'):\n"
            "    sys.exit(1)\n"
            "if sql == 'SHOW FRONTENDS':\n"
            "    table = base / ('frontends-' + host)\n"
            "    if not table.exists():\n"
            "        table = base / 'frontends'\n"
            "    print(table.read_text(), end='')\n"
            "elif sql == 'SHOW BACKENDS':\n"
            "    print('BackendId\\tHost\\tHeartbeatPort\\tAlive')\n"
            "    if (base / 'registered').exists():\n"
            "        print('10001\\t10.0.0.3\\t9050\\tfalse')\n"
            "elif sql.startswith('ALTER SYSTEM ADD BACKEND'):\n"
            "    (base / 'registered').touch()\n"
            "elif sql.startswith('ALTER SYSTEM ADD FOLLOWER'):\n"
            "    with (base / 'frontends').open('a') as f:\n"
            "        f.write('fe3\\tfalse\\t10.0.0.3\\t9010\\t8030\\t9030\\t9020\\tFOLLOWER\\t123\\ttrue\\tfalse\\t0\\tNULL\\tNULL\\tfalse\\t\\t4.1.4\\tNo\\n')\n"
            "else:\n"
            "    sys.exit(2)\n"
        )
        mysql.chmod(0o755)
        (self.base / "frontends").write_text(FRONTENDS)
        self.env = dict(
            os.environ,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            TEST_BASE=str(self.base),
            DORIS_HOME=str(self.base / "image"),
            ALLOC_DATA=str(self.data),
            DORIS_MY_CNF=str(self.base / "my.cnf"),
            ROOT_PASSWORD_HASH_FILE=str(self.base / "hash"),
            NODE_KIND="fe",
            NODE_IP="10.0.0.3",
            BOOTSTRAP_IP="10.0.0.1",
            FE_CANDIDATES="10.0.0.1 10.0.0.2 10.0.0.3",
            DISCOVERY_TIMEOUT="1",
            POLL_INTERVAL="0.1",
        )

    def run_prestart(self):
        return subprocess.run(
            ["bash", str(ROOT / "scripts/prestart.sh")], env=self.env,
            text=True, capture_output=True, timeout=10,
        )

    def endpoint(self):
        return subprocess.check_output(
            ["bash", "-c", 'printf "%s" "$FE_MASTER_IP"'],
            env=dict(self.env, BASH_ENV=str(self.data / "endpoint.env")), text=True,
        )

    def test_new_fe_uses_surviving_master_and_registers(self):
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")
        self.assertIn("10.0.0.3\t9010", (self.base / "frontends").read_text())
        conf = (self.data / "conf/fe.conf").read_text()
        self.assertIn("custom_option = preserved", conf)
        self.assertIn("initial_root_password = *" + "A" * 40, conf)

    def test_existing_fe_starts_without_reachable_peers(self):
        image = self.meta / "image"
        image.mkdir()
        (image / "ROLE").touch()
        (image / "VERSION").touch()
        self.env["TEST_OFFLINE"] = "1"
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.3")

    def test_vault_password_generates_initial_hash(self):
        password = self.base / "root-password"
        password.write_bytes(b"root@123")
        self.env["ROOT_PASSWORD_FILE"] = str(password)
        (self.base / "hash").unlink()
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("initial_root_password = *A00C34073A26B40AB4307650BFB9309D6BFA6999",
                      (self.data / "conf/fe.conf").read_text())
        self.assertNotIn("root@123", result.stdout + result.stderr)

    def test_component_config_is_appended_to_image_defaults(self):
        overrides = self.base / "overrides.conf"
        overrides.write_text("sys_log_level = WARN\n")
        self.env["CONFIG_OVERRIDES_FILE"] = str(overrides)
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        conf = (self.data / "conf/fe.conf").read_text()
        self.assertIn("custom_option = preserved", conf)
        self.assertIn("sys_log_level = WARN", conf)

    def test_component_config_cannot_change_membership_ports(self):
        overrides = self.base / "overrides.conf"
        overrides.write_text("query_port = 9999\n")
        self.env["CONFIG_OVERRIDES_FILE"] = str(overrides)
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed", result.stderr)

    def test_component_config_rejects_alternate_java_property_syntax(self):
        overrides = self.base / "overrides.conf"
        self.env["CONFIG_OVERRIDES_FILE"] = str(overrides)
        for content in ("query_port: 9999\n", "query_port 9999\n",
                        "query\\_port = 9999\n", "query_\\\nport = 9999\n",
                        "sys_log_level = INFO\\\nquery_port = 9999\n"):
            with self.subTest(content=content):
                overrides.write_text(content)
                result = self.run_prestart()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("single-line", result.stderr)

    def test_seed_returns_master_outside_seed_list(self):
        self.env["TEST_REACHABLE_HOSTS"] = "10.0.0.2 10.0.0.4"
        (self.base / "frontends").write_text(FRONTENDS.replace("10.0.0.2", "10.0.0.4"))
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.4")

    def test_leadership_changes_between_discovery_and_verification(self):
        self.env["TEST_REACHABLE_HOSTS"] = "10.0.0.1 10.0.0.2 10.0.0.4"
        (self.base / "frontends-10.0.0.1").write_text(FRONTENDS)
        changed = FRONTENDS.replace("10.0.0.2", "10.0.0.4")
        (self.base / "frontends-10.0.0.2").write_text(changed)
        (self.base / "frontends").write_text(changed)
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.4")

    def test_partial_metadata_fails(self):
        (self.meta / "image").mkdir()
        (self.meta / "image/ROLE").touch()
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete", result.stderr)

    def test_bootstrap_requires_and_consumes_explicit_permit(self):
        self.env.update(NODE_IP="10.0.0.1", TEST_OFFLINE="1")
        denied = self.run_prestart()
        self.assertNotEqual(denied.returncode, 0)
        (self.meta / ".bootstrap-approved").touch()
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.1")
        self.assertFalse((self.meta / ".bootstrap-approved").exists())
        self.assertTrue((self.meta / ".bootstrap-consumed").exists())
        self.assertNotEqual(self.run_prestart().returncode, 0)

    def test_existing_cluster_wins_over_bootstrap_permit(self):
        self.env["NODE_IP"] = "10.0.0.1"
        (self.meta / ".bootstrap-approved").touch()
        # Existing cluster doesn't contain fe1. The fake registration adds fe3,
        # so registration verification must fail rather than create a cluster.
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.meta / ".bootstrap-approved").exists())

    def test_be_registers_before_upstream_start(self):
        self.env["NODE_KIND"] = "be"
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")
        self.assertTrue((self.base / "registered").exists())

    def test_no_master_times_out_without_endpoint(self):
        self.env["TEST_OFFLINE"] = "1"
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.data / "endpoint.env").exists())

    def test_malformed_membership_is_not_readiness(self):
        (self.base / "frontends").write_text("ERROR 1045 (28000): Access denied\n")
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.data / "endpoint.env").exists())


if __name__ == "__main__":
    unittest.main()
