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

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackTest(unittest.TestCase):
    def render(self, extra=()):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                ["nomad-pack", "render", str(ROOT), "-f", str(ROOT / "examples/cluster.hcl"),
                 "--to-dir", tmp, "--auto-approve", *extra],
                check=True, capture_output=True, text=True, cwd="/tmp",
            )
            rendered = Path(tmp) / "doris/doris.nomad"
            result = subprocess.run(
                ["nomad", "job", "run", "-output", str(rendered)],
                check=True, capture_output=True, text=True,
            )
            return json.loads(result.stdout)["Job"]

    def test_unchanged_entrypoint_and_shared_prestart_output(self):
        job = self.render()
        self.assertEqual(len(job["TaskGroups"]), 6)
        for group in job["TaskGroups"]:
            tasks = {t["Name"]: t for t in group["Tasks"]}
            prepare, main = tasks["prepare"], tasks["doris"]
            self.assertEqual(prepare["Lifecycle"], {"Hook": "prestart", "Sidecar": False})
            for key in ("entrypoint", "command", "args"):
                self.assertNotIn(key, main["Config"])
            self.assertEqual(main["Env"]["BASH_ENV"], "/alloc/data/endpoint.env")
            self.assertIn("secrets/my.cnf:/root/.my.cnf:ro", main["Config"]["volumes"])
            self.assertEqual(group["RestartPolicy"]["Attempts"], 0)
            self.assertEqual(group["RestartPolicy"]["Mode"], "fail")
            templates = {t["DestPath"]: t for t in prepare["Templates"]}
            # HCL interpolation/JSON escaping must reproduce the real script.
            self.assertEqual(templates["local/prestart.sh"]["EmbeddedTmpl"],
                             (ROOT / "scripts/prestart.sh").read_text())
            self.assertEqual(templates["secrets/my.cnf"]["Perms"], "0600")

    def test_adding_fe_does_not_change_existing_groups(self):
        before = self.render()
        nodes = [dict(ip=f"10.0.0.{10+i}", hostname=f"doris-{i}", volume="doris-fe",
                      image="apache/doris:fe-4.1.4") for i in range(1, 5)]
        # JSON is valid as the expression part of a --var override.
        after = self.render(["--var", "fe_nodes=" + json.dumps(nodes)])
        existing = {g["Name"]: g for g in before["TaskGroups"]}
        expanded = {g["Name"]: g for g in after["TaskGroups"]}
        self.assertEqual(len(expanded), 7)
        for name, group in existing.items():
            self.assertEqual(group, expanded[name], name)

    def test_changing_fe_config_does_not_change_be_groups(self):
        before = self.render()
        after = self.render(["--var", "fe_config=sys_log_level = WARN\n"])
        before_groups = {g["Name"]: g for g in before["TaskGroups"]}
        for group in after["TaskGroups"]:
            if group["Name"].startswith("be-"):
                self.assertEqual(group, before_groups[group["Name"]])
            else:
                self.assertNotEqual(group, before_groups[group["Name"]])
                prepare = next(t for t in group["Tasks"] if t["Name"] == "prepare")
                config = next(t for t in prepare["Templates"] if t["DestPath"] == "local/fe-overrides.conf")
                self.assertEqual(config["EmbeddedTmpl"], "sys_log_level = WARN\n")

    def test_vault_credentials_are_runtime_templates(self):
        job = self.render()
        for group in job["TaskGroups"]:
            for task in group["Tasks"]:
                self.assertFalse(task["Vault"]["Env"])
                self.assertTrue(task["Vault"]["DisableFile"])
                templates = {t["DestPath"]: t for t in task["Templates"]}
                self.assertIn('secret "kv-data/data/doris-secret/bootstrap"',
                              templates["secrets/my.cnf"]["EmbeddedTmpl"])
                if task["Name"] == "prepare" and group["Name"].startswith("fe-"):
                    self.assertIn("secrets/root-password", templates)
                else:
                    self.assertNotIn("secrets/root-password", templates)

    def test_nomad_variable_backend_remains_available(self):
        job = self.render(["--var", "credential_source=nomad"])
        for group in job["TaskGroups"]:
            for task in group["Tasks"]:
                self.assertIsNone(task["Vault"])
                templates = {t["DestPath"]: t for t in task["Templates"]}
                self.assertIn('nomadVar "nomad/jobs/doris"',
                              templates["secrets/my.cnf"]["EmbeddedTmpl"])
                self.assertNotIn("secrets/root-password", templates)
                if task["Name"] == "prepare" and group["Name"].startswith("fe-"):
                    self.assertIn("secrets/root-password-hash", templates)


if __name__ == "__main__":
    unittest.main()
