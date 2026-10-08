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
import time
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
# The node under test (10.0.0.3) as a registered follower; its row exists as
# soon as ALTER SYSTEM ADD FOLLOWER runs, whether or not it ever joined BDB.
SELF_ROW = (
    "fe3\tfalse\t10.0.0.3\t9010\t8030\t9030\t9020\tFOLLOWER\t123\ttrue\tfalse\t"
    "0\tN/A\tN/A\tfalse\t\t4.1.4\tNo\n"
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
        # Rendered from Vault by the prepare task's secrets/root-password template.
        (self.base / "root-password").write_bytes(b"root@123")
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
            "with (base / 'queries').open('a') as f:\n"
            "    f.write(sql + '\\n')\n"
            "if host not in os.environ.get('TEST_REACHABLE_HOSTS', '10.0.0.2').split() or os.environ.get('TEST_OFFLINE'):\n"
            "    sys.exit(1)\n"
            "if sql == 'SHOW FRONTENDS':\n"
            "    table = base / ('frontends-' + host)\n"
            "    if not table.exists():\n"
            "        table = base / 'frontends'\n"
            "    print(table.read_text(), end='')\n"
            "elif sql == 'SELECT UNIX_TIMESTAMP()':\n"
            "    import time\n"
            "    print('UNIX_TIMESTAMP()')\n"
            "    print(int(time.time()) + int(os.environ.get('TEST_MASTER_CLOCK_OFFSET', '0')))\n"
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
            ROOT_PASSWORD_FILE=str(self.base / "root-password"),
            NODE_KIND="fe",
            NODE_IP="10.0.0.3",
            BOOTSTRAP_IP="10.0.0.1",
            FE_CANDIDATES="10.0.0.1 10.0.0.2 10.0.0.3",
            # Bash SECONDS counts whole wall-clock seconds, so a deadline of 1
            # can expire almost immediately. 2 leaves at least one second.
            DISCOVERY_TIMEOUT="2",
            EXISTING_FE_DISCOVERY_TIMEOUT="2",
            POLL_INTERVAL="0.1",
        )

    def write_identity(self, *files):
        if not files:
            return
        image = self.meta / "image"
        image.mkdir(exist_ok=True)
        for name in files:
            (image / name).touch()

    def run_prestart(self):
        return subprocess.run(
            ["bash", str(ROOT / "scripts/prestart.sh")], env=self.env,
            text=True, capture_output=True, timeout=10,
        )

    def queries(self):
        path = self.base / "queries"
        return path.read_text().splitlines() if path.exists() else []

    def endpoint(self):
        return subprocess.check_output(
            ["bash", "-c", 'printf "%s" "$FE_MASTER_IP"'],
            env=dict(self.env, BASH_ENV=str(self.data / "endpoint.env")), text=True,
        )

    def test_new_fe_uses_surviving_master_and_leaves_registration(self):
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")
        # init_fe.sh registers the follower; prestart only reads membership
        # and compares clocks.
        self.assertEqual(set(self.queries()), {"SHOW FRONTENDS", "SELECT UNIX_TIMESTAMP()"})
        conf = (self.data / "conf/fe.conf").read_text()
        self.assertIn("custom_option = preserved", conf)
        # MySQL PASSWORD("root@123"), as documented by Doris.
        self.assertIn("initial_root_password = *A00C34073A26B40AB4307650BFB9309D6BFA6999", conf)
        self.assertNotIn("root@123", result.stdout + result.stderr + conf)
        # Read after fe.conf, so init_fe.sh's appended /24 cannot widen it.
        self.assertEqual((self.data / "conf/fe_custom.conf").read_text(),
                         "priority_networks = 10.0.0.3/32\n")

    def test_existing_fe_uses_elected_master_as_helper(self):
        # A member that was interrupted after ROLE/VERSION but before joining
        # BDB can only resume through a real helper; a healthy member ignores
        # the helper's role information and reads its local group.
        self.write_identity("ROLE", "VERSION")
        (self.base / "frontends").write_text(FRONTENDS + SELF_ROW)
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")

    def test_existing_fe_that_is_master_uses_itself(self):
        self.write_identity("ROLE", "VERSION")
        self.env["TEST_REACHABLE_HOSTS"] = "10.0.0.3"
        (self.base / "frontends").write_text(FRONTENDS.replace("10.0.0.2", "10.0.0.3"))
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.3")

    def test_existing_fe_without_reachable_peers_uses_a_peer_as_helper(self):
        # Cold start: do not wait for the full discovery timeout, and never
        # name itself as helper while another FE exists. BDB creates a new
        # group only when the helper is the node itself, so a half-joined
        # node fails and is rescheduled instead of becoming a second cluster.
        self.write_identity("ROLE", "VERSION")
        self.env.update(TEST_OFFLINE="1", DISCOVERY_TIMEOUT="6")
        started = time.monotonic()
        result = self.run_prestart()
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.1")
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 5)

    def test_single_fe_cluster_restarts_as_its_own_helper(self):
        self.write_identity("ROLE", "VERSION")
        self.env.update(TEST_OFFLINE="1", FE_CANDIDATES="10.0.0.3")
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.3")

    def test_half_joined_fe_with_only_role_resumes_from_master(self):
        # ROLE is written before VERSION is downloaded; with a real helper the
        # FE repeats the download instead of failing forever.
        self.write_identity("ROLE")
        (self.base / "frontends").write_text(FRONTENDS + SELF_ROW)
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")

    def test_half_joined_fe_without_master_uses_a_peer_as_helper(self):
        self.write_identity("ROLE")
        self.env["TEST_OFFLINE"] = "1"
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.1")

    def test_existing_fe_missing_from_master_list_fails(self):
        # Removed from the cluster or started with a changed IP: starting it
        # with any helper would rejoin or recreate a group. Operator decision.
        self.write_identity("ROLE", "VERSION")
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not listed", result.stderr)
        self.assertFalse((self.data / "endpoint.env").exists())

    def test_clock_skew_with_master_fails_before_any_metadata(self):
        # BDB rejects a replica whose clock differs from the master's by more
        # than max_bdbje_clock_delta_ms (5 s) only after ROLE and VERSION are
        # written. Refuse earlier, with the cause in the allocation log.
        self.env["TEST_MASTER_CLOCK_OFFSET"] = "16"
        for identity in ((), ("ROLE", "VERSION")):
            with self.subTest(identity=identity):
                self.write_identity(*identity)
                (self.base / "frontends").write_text(FRONTENDS + SELF_ROW)
                result = self.run_prestart()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("clock", result.stderr)
                self.assertIn("16", result.stderr)
                self.assertFalse((self.data / "endpoint.env").exists())

    def test_clock_skew_within_threshold_passes(self):
        self.env["TEST_MASTER_CLOCK_OFFSET"] = "-3"
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")

    def test_empty_vault_password_fails(self):
        (self.base / "root-password").write_bytes(b"")
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty", result.stderr)
        self.assertFalse((self.data / "endpoint.env").exists())

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

    def test_consul_registered_fe_extends_static_seeds(self):
        # The only reachable FE is registered in Consul but is not a seed.
        self.env["TEST_REACHABLE_HOSTS"] = "10.0.0.5"
        (self.base / "frontends").write_text(FRONTENDS.replace("10.0.0.2", "10.0.0.5"))
        consul = self.base / "consul-fe"
        consul.write_text("10.0.0.5\n10.0.0.1\n\n")
        self.env["CONSUL_FE_FILE"] = str(consul)
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.5")

    def test_empty_consul_catalog_uses_static_seeds(self):
        consul = self.base / "consul-fe"
        consul.write_text("\n")
        self.env["CONSUL_FE_FILE"] = str(consul)
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")

    def test_invalid_consul_address_fails(self):
        consul = self.base / "consul-fe"
        consul.write_text("doris-fe.example\n")
        self.env["CONSUL_FE_FILE"] = str(consul)
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid IPv4", result.stderr)

    def test_leadership_changes_between_discovery_and_verification(self):
        self.env["TEST_REACHABLE_HOSTS"] = "10.0.0.1 10.0.0.2 10.0.0.4"
        (self.base / "frontends-10.0.0.1").write_text(FRONTENDS)
        changed = FRONTENDS.replace("10.0.0.2", "10.0.0.4")
        (self.base / "frontends-10.0.0.2").write_text(changed)
        (self.base / "frontends").write_text(changed)
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.4")

    def test_image_without_identity_fails(self):
        # Doris writes ROLE before anything else under image/; an image
        # directory without it is not a state a new node may start over.
        (self.meta / "image").mkdir()
        (self.meta / "image/image.0").touch()
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete", result.stderr)
        self.assertFalse((self.data / "endpoint.env").exists())

    def test_bootstrap_requires_and_consumes_explicit_permit(self):
        self.env.update(NODE_IP="10.0.0.1", TEST_OFFLINE="1",
                        META_VOLUME="doris-fe-b0-d0", NODE_NAME="doris-1",
                        NOMAD_JOB_NAME="doris")
        denied = self.run_prestart()
        self.assertNotEqual(denied.returncode, 0)
        # The operator is told exactly where the permit goes.
        self.assertIn("create .bootstrap-approved in host volume doris-fe-b0-d0 "
                      "on Nomad node doris-1", denied.stderr)
        self.assertIn("stop job doris", denied.stderr)
        self.assertIn("scripts/bootstrap-permit.sh from the bastion with the same arguments as nomad-pack run", denied.stderr)
        (self.meta / ".bootstrap-approved").touch()
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.1")
        self.assertFalse((self.meta / ".bootstrap-approved").exists())
        self.assertTrue((self.meta / ".bootstrap-consumed").exists())
        again = self.run_prestart()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already consumed", again.stderr)

    def test_existing_cluster_wins_over_bootstrap_permit(self):
        self.env["NODE_IP"] = "10.0.0.1"
        (self.meta / ".bootstrap-approved").touch()
        # An elected master exists, so the designated FE joins it as a
        # follower instead of consuming the permit and creating a cluster.
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")
        self.assertTrue((self.meta / ".bootstrap-approved").exists())
        self.assertFalse((self.meta / ".bootstrap-consumed").exists())

    def test_new_fe_reported_as_master_fails(self):
        self.env["TEST_REACHABLE_HOSTS"] = "10.0.0.2 10.0.0.3"
        (self.base / "frontends").write_text(FRONTENDS.replace("10.0.0.2", "10.0.0.3"))
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without metadata", result.stderr)
        self.assertFalse((self.data / "endpoint.env").exists())

    def test_be_leaves_registration_to_upstream_entrypoint(self):
        self.env.update(NODE_KIND="be", BE_DISK_COUNT="2")
        result = self.run_prestart()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.endpoint(), "10.0.0.2")
        # init_be.sh runs SHOW BACKENDS / ALTER SYSTEM ADD BACKEND itself.
        self.assertEqual(set(self.queries()), {"SHOW FRONTENDS"})
        # One storage root per mounted disk, never the unmounted parent.
        storage = self.base / "image/be/storage"
        self.assertIn(f"storage_root_path = {storage}/data1;{storage}/data2\n",
                      (self.data / "conf/be.conf").read_text())
        # Read after be.conf, so init_be.sh's appended /24 cannot widen it.
        self.assertEqual((self.data / "conf/be_custom.conf").read_text(),
                         "priority_networks = 10.0.0.3/32\n")

    def test_be_requires_disk_count(self):
        self.env["NODE_KIND"] = "be"
        result = self.run_prestart()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.data / "endpoint.env").exists())

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
