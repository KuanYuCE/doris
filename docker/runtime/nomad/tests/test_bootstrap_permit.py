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
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bootstrap-permit.sh"
EXAMPLE = ROOT / "examples/cluster.hcl"
NODE_ID = "4659bfec-bed2-0599-d0dd-b78531c400f1"


class BootstrapPermitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.volume = self.base / "fe-meta"
        self.volume.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        # Only Nomad, ssh, sudo and ip are replaced; the pack is rendered by the
        # real nomad-pack from the real templates.
        nomad = self.bin / "nomad"
        nomad.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "flags = [a for a in sys.argv[1:] if a.startswith('-') and '=' in a]\n"
            "args = [a for a in sys.argv[1:] if a not in flags]\n"
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
            "elif args[:2] == ['node', 'status'] and len(args) == 5 and 'index .HostVolumes' not in args[3]:\n"
            "    print(env.get('TEST_NODE_VOLUMES', 'doris-fe-b0-d0 doris-fe-b0-d1 '), end='')\n"
            "elif args[:2] == ['node', 'status'] and len(args) == 5:\n"
            "    print(env['TEST_NODE_INFO'], end='')\n"
            "elif args[:2] == ['node', 'status']:\n"
            "    print(env['TEST_NODE_IDS'], end='')\n"
            "else:\n"
            "    sys.exit(3)\n"
        )
        nomad.chmod(0o755)
        ip = self.bin / "ip"
        ip.write_text(
            "#!/usr/bin/env bash\n"
            "for a in $TEST_LOCAL_ADDRS; do echo \"2: eth0    inet $a/24 brd x scope global eth0\"; done\n"
        )
        ip.chmod(0o755)
        # The bastion's ssh: record the target, then run the remote command
        # here with the script on stdin, as sshd would hand it to a shell.
        ssh = self.bin / "ssh"
        ssh.write_text(
            "#!/usr/bin/env bash\n"
            "[[ $1 == -- ]] && shift\n"
            "echo \"$1\" >> \"$TEST_BASE/ssh\"\n"
            "[[ -z ${TEST_SSH_DOWN:-} ]] || exit 255\n"
            "exec bash -c \"$2\"\n"
        )
        ssh.chmod(0o755)
        sudo = self.bin / "sudo"
        sudo.write_text(
            "#!/usr/bin/env bash\n"
            "[[ $1 == -n ]] || exit 9\n"
            "shift\n"
            "[[ -z ${TEST_SUDO_NEEDS_PASSWORD:-} ]] || { echo 'sudo: a password is required' >&2; exit 1; }\n"
            "exec \"$@\"\n"
        )
        sudo.chmod(0o755)
        self.ca = self.base / "ca.pem"
        self.ca.write_text("test CA\n")
        self.env = dict(
            {k: v for k, v in os.environ.items() if not k.startswith("NOMAD_")},
            PATH=f"{self.bin}:{os.environ['PATH']}",
            TEST_BASE=str(self.base),
            TEST_JOB="absent",
            TEST_NODE_IDS=NODE_ID + "\n",
            TEST_NODE_INFO=f"10.0.0.11:4646 {self.volume}",
            # The FE and BE IP aliases of the bootstrap host.
            TEST_LOCAL_ADDRS="127.0.0.1 10.0.0.11 10.0.0.21",
        )

    def run_permit(self, *args, env=None, script=SCRIPT, var_files=(EXAMPLE,)):
        return subprocess.run(
            ["bash", str(script), *args, *(f"--var-file={f}" for f in var_files)],
            env=dict(self.env, **(env or {})), text=True, capture_output=True,
            timeout=60, cwd=self.base,
        )

    def ssh_targets(self):
        path = self.base / "ssh"
        return path.read_text().splitlines() if path.exists() else []

    def calls(self):
        return (self.base / "calls").read_text().splitlines()

    def assert_no_permit(self):
        self.assertFalse((self.volume / ".bootstrap-approved").exists())

    def test_dry_run_prints_location_before_the_job_exists(self):
        result = self.run_permit("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("node:   doris-1 ", result.stdout)
        self.assertIn("volume: doris-fe-b0-d0", result.stdout)
        self.assertIn(f"permit: {self.volume}/.bootstrap-approved", result.stdout)
        self.assertEqual(self.ssh_targets(), [])
        self.assert_no_permit()
        # The printed command is the same invocation without --dry-run.
        rerun = result.stdout.splitlines()[-1]
        self.assertIn(f"--var-file={EXAMPLE}", rerun)
        self.assertNotIn("--dry-run", rerun)

    def test_creates_permit_over_ssh_to_node_address(self):
        result = self.run_permit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.volume / ".bootstrap-approved").is_file())
        self.assertEqual(self.ssh_targets(), ["10.0.0.11"])
        self.assertIn("Next: nomad-pack run", result.stdout)

    def test_runs_standalone(self):
        # Copied alone to a bastion: no other file of the pack's scripts/ is read.
        alone = self.base / "bastion" / "bootstrap-permit.sh"
        alone.parent.mkdir()
        shutil.copy(SCRIPT, alone)
        # The pack is the positional argument, as for nomad-pack run.
        result = self.run_permit(str(ROOT), script=alone)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.volume / ".bootstrap-approved").is_file())
        # Without --pack it looks for the pack around itself and says so.
        missing = self.run_permit("--dry-run", script=alone)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("nomad-pack render failed", missing.stderr)

    def test_copied_functions_match_ops_common(self):
        # bootstrap-permit.sh carries its own copy of the shared functions.
        def functions(text):
            return dict(re.findall(r"^([a-z_]+)\(\) \{\n(.*?)^\}\n", text, re.M | re.S))
        shared = functions((ROOT / "scripts/ops-common.sh").read_text())
        copied = functions(SCRIPT.read_text())
        self.assertTrue(shared)
        for name, body in shared.items():
            self.assertEqual(copied.get(name), body, name)

    def test_refuses_while_job_is_running(self):
        # The permit must be in place before the job starts.
        for job, allocs in (("doris false", ""), ("doris true", "complete\nrunning\n"),
                            ("doris true", "pending\n")):
            with self.subTest(job=job, allocs=allocs):
                result = self.run_permit(env={"TEST_JOB": job, "TEST_ALLOCS": allocs})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.ssh_targets(), [])
                self.assert_no_permit()

    def test_stopped_or_unrelated_job_is_allowed(self):
        # Stopped (e.g. after wipe-volumes.sh), or a prefix match of another job.
        for job in ("doris true", "doris-test false"):
            with self.subTest(job=job):
                result = self.run_permit("--dry-run", env={"TEST_JOB": job, "TEST_ALLOCS": "complete\n"})
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_job_state_stops(self):
        result = self.run_permit(env={"TEST_JOB": "error"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot check job", result.stderr)
        self.assert_no_permit()

    def test_checks_every_namespace_the_job_may_live_in(self):
        # The rendered namespace, --namespace (either spelling) and
        # NOMAD_NAMESPACE are all checked, whichever nomad-pack run applies.
        result = self.run_permit("--dry-run", "--namespace=doris", "-namespace=team",
                                 env={"NOMAD_NAMESPACE": "env-ns"})
        self.assertEqual(result.returncode, 0, result.stderr)
        inspected = [c for c in self.calls() if c.startswith("job inspect")]
        for ns in ("default", "doris", "team", "env-ns"):
            self.assertEqual(sum(f"-namespace={ns}" in c for c in inspected), 1, ns)

    def test_running_in_flag_namespace_is_refused(self):
        # The var file says "default", but the job runs where --namespace put it.
        result = self.run_permit("--namespace=doris", env={"TEST_JOB_NS_doris": "doris false"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nomad job stop -namespace=doris doris", result.stderr)
        self.assert_no_permit()

    def test_nomad_pack_arguments_are_forwarded_in_order(self):
        # A recording nomad-pack: render arguments only, in the given order;
        # connection flags stay with the nomad CLI.
        log = self.base / "pack-args"
        wrapper = self.bin / "nomad-pack"
        wrapper.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {log}\nexit 1\n')
        wrapper.chmod(0o755)
        override = self.base / "override.hcl"
        override.write_text("")
        result = self.run_permit("--registry=doris_packs", "--address=https://nomad:4646",
                                 "--var=fe_cpu=3000", "--ref=pack-template",
                                 f"--ca-cert={self.ca}", "--namespace=doris", "doris",
                                 var_files=(EXAMPLE, override))
        self.assertNotEqual(result.returncode, 0)
        args = log.read_text().splitlines()
        self.assertEqual(args[0], "render")
        self.assertEqual(args[1], "doris")
        rest = args[2:args.index("--to-dir")]
        self.assertEqual(rest, ["--registry=doris_packs", "--var=fe_cpu=3000", "--ref=pack-template",
                                f"--var-file={EXAMPLE}", f"--var-file={override}"])

    def test_stale_env_ca_does_not_break_the_render(self):
        # A --ca-cert flag wins over a stale NOMAD_CACERT, as in nomad-pack run;
        # the render itself never sees the connection settings.
        result = self.run_permit("--dry-run", f"--ca-cert={self.ca}",
                                 env={"NOMAD_CACERT": "ca.pem.missing", "NOMAD_TOKEN": "t"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_tls_file_is_named(self):
        result = self.run_permit("--dry-run", env={"NOMAD_CACERT": "ca.pem.missing"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read ca.pem.missing (from NOMAD_CACERT)", result.stderr)
        result = self.run_permit("--dry-run", "--client-key=key.missing")
        self.assertIn("Cannot read key.missing (from --client-key)", result.stderr)
        self.assertEqual(self.ssh_targets(), [])

    def test_var_flag_changes_the_render(self):
        result = self.run_permit("--dry-run", "--var=bootstrap_fe=10.0.0.13")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("node:   doris-3 ", result.stdout)

    def test_later_var_file_selects_another_bootstrap_fe(self):
        override = self.base / "override.hcl"
        override.write_text('bootstrap_fe = "10.0.0.12"\n')
        result = self.run_permit("--dry-run", var_files=(EXAMPLE, override))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("node:   doris-2 ", result.stdout)

    def test_requires_variables_and_one_pack(self):
        self.assertIn("usage", self.run_permit("--dry-run", var_files=()).stderr)
        result = self.run_permit("--dry-run", str(ROOT), str(ROOT))
        self.assertIn("More than one pack", result.stderr)

    def test_invalid_var_file_fails_in_render(self):
        bad = self.base / "bad.hcl"
        bad.write_text('bootstrap_fe = "10.0.0.99"\n')
        result = self.run_permit("--dry-run", var_files=(EXAMPLE, bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nomad-pack render failed", result.stderr)
        # nomad-pack prints the reason on stdout; it must reach the operator.
        self.assertIn("bootstrap_fe must occur in fe_nodes", result.stderr)

    def test_ssh_host_override(self):
        result = self.run_permit("--ssh-host=ops@doris-1.example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ssh_targets(), ["ops@doris-1.example"])
        self.assertTrue((self.volume / ".bootstrap-approved").is_file())

    def test_ssh_host_cannot_be_an_option(self):
        result = self.run_permit("--ssh-host=-oProxyCommand=x")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ssh_targets(), [])

    def test_ssh_or_sudo_failure_is_reported(self):
        for key in ("TEST_SSH_DOWN", "TEST_SUDO_NEEDS_PASSWORD"):
            with self.subTest(key=key):
                result = self.run_permit(env={key: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Remote step on 10.0.0.11 failed", result.stderr)
                self.assert_no_permit()

    def test_remote_path_is_passed_as_data(self):
        # A hostile path must reach the remote side as one literal argument.
        odd = self.base / "fe meta; touch pwned"
        odd.mkdir()
        result = self.run_permit(env={"TEST_NODE_INFO": f"10.0.0.11:4646 {odd}"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((odd / ".bootstrap-approved").is_file())
        self.assertFalse((self.base / "pwned").exists())

    def test_refuses_other_machine(self):
        # The SSH target (e.g. a wrong --ssh-host) must own the node's address;
        # identical volume layouts would otherwise put a stray permit elsewhere.
        for addrs in ("127.0.0.1 10.0.0.12 10.0.0.22", "10.0.0.111"):
            with self.subTest(addrs=addrs):
                result = self.run_permit(env={"TEST_LOCAL_ADDRS": addrs})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("does not own 10.0.0.11", result.stderr)
                self.assert_no_permit()

    def test_refuses_used_volume(self):
        for marker in (".bootstrap-consumed", "image", "bdb"):
            with self.subTest(marker=marker):
                path = self.volume / marker
                path.mkdir()
                result = self.run_permit()
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_permit()
                path.rmdir()

    def test_existing_permit_is_kept(self):
        (self.volume / ".bootstrap-approved").touch()
        result = self.run_permit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Permit already present", result.stdout)

    def test_requires_unique_node_and_known_volume(self):
        result = self.run_permit(env={"TEST_NODE_IDS": f"{NODE_ID}\n{NODE_ID}\n"})
        self.assertIn("exactly one Nomad node", result.stderr)
        result = self.run_permit(env={"TEST_NODE_INFO": "10.0.0.11:4646 ",
                                      "TEST_NODE_VOLUMES": "doris-fe-b1-d0 doris-fe-b1-d1 "})
        # Names what the node does have, so a block_index mismatch is obvious.
        self.assertIn("Node doris-1 (" + NODE_ID + ") has no host volume doris-fe-b0-d0; "
                      "it has: doris-fe-b1-d0 doris-fe-b1-d1.", result.stderr)
        # A failed lookup must stop the script, not continue with an empty path.
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ssh_targets(), [])

    def test_connection_flags_reach_every_nomad_call(self):
        # nomad-pack style --flag= is accepted and handed to nomad as -flag=.
        flags = ["--address=https://nomad.service:4646", f"-ca-cert={self.ca}",
                 "--tls-server-name=server.global.nomad"]
        result = self.run_permit(*flags)
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.calls():
            for flag in flags:
                self.assertIn("-" + flag.lstrip("-"), call.split())

    def test_token_is_never_accepted_in_argv(self):
        for flag in ("-token=secret", "--token=secret", "-token"):
            with self.subTest(flag=flag):
                result = self.run_permit(flag)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("NOMAD_TOKEN", result.stderr)
                self.assertNotIn("secret", result.stdout + result.stderr)

    def test_unknown_flag_is_rejected(self):
        self.assertIn("usage", self.run_permit("-bogus=1").stderr)


if __name__ == "__main__":
    unittest.main()
