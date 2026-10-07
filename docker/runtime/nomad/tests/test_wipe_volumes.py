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
VOLUMES = ("doris-fe-b0-d0", "doris-fe-b0-d1", "doris-be-b0-d0", "doris-be-b0-d1")


class WipeVolumesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        # Two nodes, each with one FE and one BE, rendered by the real pack.
        self.var_file = self.base / "cluster.hcl"
        self.var_file.write_text(
            'job_name = "doris"\n'
            'namespace = "data"\n'
            'fe_image_repo = "apache/doris"\n'
            'be_image_repo = "apache/doris"\n'
            'bootstrap_fe = "10.0.0.11"\n'
            'discovery_fe_ips = ["10.0.0.11"]\n'
            'fe_nodes = [\n'
            '  { ip = "10.0.0.11", hostname = "doris-1", block_index = 0 },\n'
            '  { ip = "10.0.0.12", hostname = "doris-2", block_index = 0 },\n'
            ']\n'
            'be_nodes = [\n'
            '  { ip = "10.0.0.21", hostname = "doris-1", block_index = 0 },\n'
            '  { ip = "10.0.0.22", hostname = "doris-2", block_index = 0 },\n'
            ']\n'
        )
        # Volumes live under a two-level root so the top-level guard passes.
        self.root = self.base / "srv" / "doris"
        for node in ("doris-1", "doris-2"):
            for volume in VOLUMES:
                path = self.root / node / volume
                (path / "data").mkdir(parents=True)
                (path / ".hidden").write_text("x")
                (path / "lost+found").mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        nomad = self.bin / "nomad"
        nomad.write_text(
            "#!/usr/bin/env python3\n"
            "import os, re, sys\n"
            "from pathlib import Path\n"
            "args = [a for a in sys.argv[1:] if not (a.startswith('-') and '=' in a)]\n"
            "flags = [a for a in sys.argv[1:] if a.startswith('-') and '=' in a]\n"
            "with (Path(os.environ['TEST_BASE']) / 'calls').open('a') as f:\n"
            "    f.write(' '.join(args[:2] + flags) + '\\n')\n"
            "env = os.environ\n"
            "ns = next((f.split('=', 1)[1] for f in flags if f.startswith('-namespace=')), '')\n"
            "job = env.get('TEST_JOB_NS_' + ns, env['TEST_JOB'])\n"
            "if args[:2] == ['job', 'inspect']:\n"
            "    if job == 'absent':\n"
            "        print('No job(s) with prefix or ID \"doris\" found', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    if job == 'error':\n"
            "        print('Error querying job: connection refused', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    print(job, end='')\n"
            "elif args[:2] == ['job', 'allocs']:\n"
            "    print(env.get('TEST_ALLOCS', ''), end='')\n"
            "elif args[:2] == ['node', 'status'] and len(args) == 5:\n"
            "    node = args[4]\n"
            "    missing = env.get('TEST_MISSING', '').split()\n"
            "    m = re.search(r'HostVolumes \"([^\"]+)\"', args[3])\n"
            "    if m is None:\n"
            "        # Listing every volume the node has.\n"
            "        print(' '.join(v for v in env['TEST_VOLUMES'].split() if v not in missing) + ' ', end='')\n"
            "        sys.exit(0)\n"
            "    volume = m.group(1)\n"
            "    path = Path(env['TEST_ROOT']) / node / volume\n"
            "    if volume in env.get('TEST_MISSING', '').split():\n"
            "        path = ''\n"
            "    if node == 'doris-1' and volume == env.get('TEST_TOP_LEVEL_VOLUME'):\n"
            "        path = '/tmp'\n"
            "    print(env['TEST_ADDR_' + node.replace('-', '_')] + ':4646 ' + str(path), end='')\n"
            "elif args[:2] == ['node', 'status']:\n"
            "    name = re.search(r'eq .Name \"([^\"]+)\"', args[3]).group(1)\n"
            "    print(name + '\\n', end='')\n"
            "else:\n"
            "    sys.exit(3)\n"
        )
        nomad.chmod(0o755)
        # ssh runs the remote command here; `ip` reports the reached host's
        # addresses, keyed by the SSH target.
        ssh = self.bin / "ssh"
        ssh.write_text(
            "#!/usr/bin/env bash\n"
            "[[ $1 == -- ]] && shift\n"
            "echo \"$1\" >> \"$TEST_BASE/ssh\"\n"
            "export TEST_TARGET=$1\n"
            "exec bash -c \"$2\"\n"
        )
        ssh.chmod(0o755)
        sudo = self.bin / "sudo"
        sudo.write_text("#!/usr/bin/env bash\n[[ $1 == -n ]] || exit 9\nshift\nexec \"$@\"\n")
        sudo.chmod(0o755)
        ip = self.bin / "ip"
        ip.write_text(
            "#!/usr/bin/env bash\n"
            "key=TEST_OWNS_${TEST_TARGET//./_}\n"
            "for a in ${!key}; do echo \"2: eth0    inet $a/24 scope global eth0\"; done\n"
        )
        ip.chmod(0o755)
        self.env = dict(
            {k: v for k, v in os.environ.items() if not k.startswith("NOMAD_")},
            PATH=f"{self.bin}:{os.environ['PATH']}",
            TEST_BASE=str(self.base),
            TEST_ROOT=str(self.root),
            TEST_JOB="doris true",
            TEST_ALLOCS="complete\nfailed\n",
            TEST_VOLUMES=" ".join(VOLUMES),
            TEST_ADDR_doris_1="10.0.1.1",
            TEST_ADDR_doris_2="10.0.1.2",
            TEST_OWNS_10_0_1_1="10.0.1.1 10.0.0.11 10.0.0.21",
            TEST_OWNS_10_0_1_2="10.0.1.2 10.0.0.12 10.0.0.22",
        )

    def wipe(self, *args):
        return subprocess.run(
            ["bash", str(ROOT / "scripts/wipe-volumes.sh"), *args, f"--var-file={self.var_file}"],
            env=self.env, text=True, capture_output=True, timeout=60, cwd=self.base,
        )

    def contents(self, node, volume):
        return sorted(p.name for p in (self.root / node / volume).iterdir())

    def assert_untouched(self):
        for node in ("doris-1", "doris-2"):
            for volume in VOLUMES:
                self.assertEqual(self.contents(node, volume), [".hidden", "data", "lost+found"])

    def ssh_targets(self):
        path = self.base / "ssh"
        return path.read_text().splitlines() if path.exists() else []

    def test_dry_run_lists_every_volume_and_deletes_nothing(self):
        result = self.wipe("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        for node in ("doris-1", "doris-2"):
            for volume in VOLUMES:
                self.assertIn(f"{self.root / node / volume}", result.stdout)
        self.assertIn("--confirm=doris", result.stdout.splitlines()[-1])
        self.assertEqual(self.ssh_targets(), [])
        self.assert_untouched()

    def test_wipes_every_volume_keeping_directories(self):
        result = self.wipe("--confirm=doris")
        self.assertEqual(result.returncode, 0, result.stderr)
        for node in ("doris-1", "doris-2"):
            for volume in VOLUMES:
                self.assertEqual(self.contents(node, volume), ["lost+found"])
        # One SSH session per node, to the address it advertises to Nomad.
        self.assertEqual(sorted(self.ssh_targets()), ["10.0.1.1", "10.0.1.2"])

    def test_render_failure_shows_the_reason(self):
        with self.var_file.open("a") as f:
            f.write('bootstrap_fe = "10.0.0.99"\n')
        result = self.wipe("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap_fe must occur in fe_nodes", result.stderr)
        self.assert_untouched()

    def test_requires_matching_confirmation(self):
        self.assertIn("usage", self.wipe().stderr)
        result = self.wipe("--confirm=prod")
        self.assertIn("does not match job doris", result.stderr)
        self.assert_untouched()

    def test_refuses_registered_running_job(self):
        self.env["TEST_JOB"] = "doris false"
        result = self.wipe("--confirm=doris")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still running", result.stderr)
        self.assert_untouched()

    def test_refuses_stopped_job_with_live_allocations(self):
        for status in ("running", "pending"):
            with self.subTest(status=status):
                self.env["TEST_ALLOCS"] = f"complete\n{status}\n"
                result = self.wipe("--confirm=doris")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("running or pending allocations", result.stderr)
                self.assert_untouched()

    def test_job_checks_cover_every_namespace(self):
        self.assertEqual(self.wipe("--dry-run", "--namespace=team").returncode, 0)
        calls = (self.base / "calls").read_text().splitlines()
        inspected = [c for c in calls if c.startswith("job inspect")]
        for ns in ("data", "team"):
            self.assertTrue(any(f"-namespace={ns}" in c for c in inspected), (ns, inspected))

    def test_running_in_flag_namespace_is_refused(self):
        self.env["TEST_JOB_NS_team"] = "doris false"
        result = self.wipe("--confirm=doris", "--namespace=team")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nomad job stop -namespace=team doris", result.stderr)
        self.assert_untouched()

    def test_absent_or_prefix_matched_job_is_not_running(self):
        for job in ("absent", "doris-test false"):
            with self.subTest(job=job):
                self.env["TEST_JOB"] = job
                result = self.wipe("--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("is not registered", result.stdout)

    def test_unknown_job_state_stops_everything(self):
        self.env["TEST_JOB"] = "error"
        result = self.wipe("--confirm=doris")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot check job", result.stderr)
        self.assert_untouched()

    def test_unresolved_volume_deletes_nothing_anywhere(self):
        # The last volume is checked before the first node is touched.
        self.env["TEST_MISSING"] = "doris-fe-b0-d1"
        result = self.wipe("--confirm=doris")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no host volume doris-fe-b0-d1; it has: "
                      "doris-fe-b0-d0 doris-be-b0-d0 doris-be-b0-d1.", result.stderr)
        self.assertEqual(self.ssh_targets(), [])
        self.assert_untouched()

    def test_remote_refuses_wrong_machine(self):
        self.env["TEST_OWNS_10_0_1_1"] = "10.0.9.9"
        result = self.wipe("--confirm=doris")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not own 10.0.1.1", result.stderr)
        for volume in VOLUMES:
            self.assertEqual(self.contents("doris-1", volume), [".hidden", "data", "lost+found"])

    def test_remote_refuses_top_level_directory(self):
        # A volume reported at a top-level path such as /srv is never emptied,
        # and no other volume on that node is touched either.
        self.env["TEST_TOP_LEVEL_VOLUME"] = "doris-fe-b0-d1"
        result = self.wipe("--confirm=doris")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to empty /tmp", result.stderr)
        self.assert_untouched()

    def test_remote_checks_every_path_before_deleting(self):
        missing = self.root / "doris-1" / "doris-be-b0-d1"
        subprocess.run(["rm", "-rf", str(missing)], check=True)
        result = self.wipe("--confirm=doris")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        # doris-1's other volumes are intact although they were listed first.
        for volume in VOLUMES[:3]:
            self.assertEqual(self.contents("doris-1", volume), [".hidden", "data", "lost+found"])


if __name__ == "__main__":
    unittest.main()
