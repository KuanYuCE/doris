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
            # Server-side rules (for example, no empty templates) are checked
            # locally even without a reachable agent.
            subprocess.run(["nomad", "job", "validate", str(rendered)],
                           check=True, capture_output=True, text=True)
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

    def test_empty_component_config_is_omitted(self):
        job = self.render(["--var", "fe_config=", "--var", "be_config="])
        for group in job["TaskGroups"]:
            prepare = next(t for t in group["Tasks"] if t["Name"] == "prepare")
            self.assertNotIn("CONFIG_OVERRIDES_FILE", prepare["Env"])
            for template in prepare["Templates"]:
                self.assertNotIn("overrides", template["DestPath"])
                self.assertTrue(template["EmbeddedTmpl"])

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

    def test_consul_services_checks_and_discovery(self):
        job = self.render()
        for group in job["TaskGroups"]:
            kind = group["Name"].split("-")[0]
            tasks = {t["Name"]: t for t in group["Tasks"]}
            prepare, main = tasks["prepare"], tasks["doris"]
            services = {s["Name"]: s for s in main["Services"]}
            expected = {f"doris-{kind}"} | ({"doris-fe-http"} if kind == "fe" else set())
            self.assertEqual(set(services), expected)
            for service in services.values():
                self.assertEqual(service["Provider"], "consul")
                self.assertEqual(service["Tags"][0], kind)
            checks = {c["Name"]: c for c in services[f"doris-{kind}"]["Checks"]}
            ready = checks["sql-ready"]
            self.assertEqual(ready["Type"], "script")
            self.assertEqual(ready["Args"][:2], ["/local/ready.sh", kind])
            if kind == "be":
                # BE readiness asks the static seeds after the prestart master.
                self.assertEqual(ready["Args"][3:], ["10.0.0.11", "10.0.0.12", "10.0.0.13"])
            else:
                self.assertEqual(len(ready["Args"]), 3)
                http = services["doris-fe-http"]["Checks"][0]
                self.assertEqual((http["Type"], http["Path"]), ("http", "/api/health"))
            templates = {t["DestPath"]: t for t in main["Templates"]}
            self.assertEqual(templates["local/ready.sh"]["EmbeddedTmpl"],
                             (ROOT / "scripts/ready.sh").read_text())
            prepare_templates = {t["DestPath"]: t for t in prepare["Templates"]}
            self.assertIn('service "doris-fe"', prepare_templates["local/consul-fe"]["EmbeddedTmpl"])
            self.assertEqual(prepare["Env"]["CONSUL_FE_FILE"], "/local/consul-fe")

    def test_nomad_service_provider_has_no_consul_features(self):
        job = self.render(["--var", "service_provider=nomad"])
        for group in job["TaskGroups"]:
            tasks = {t["Name"]: t for t in group["Tasks"]}
            prepare, main = tasks["prepare"], tasks["doris"]
            for service in main["Services"]:
                self.assertEqual(service["Provider"], "nomad")
                self.assertTrue(all(c["Type"] in ("tcp", "http") for c in service["Checks"]))
            self.assertNotIn("local/ready.sh", {t["DestPath"] for t in main["Templates"]})
            self.assertNotIn("local/consul-fe", {t["DestPath"] for t in prepare["Templates"]})
            self.assertNotIn("CONSUL_FE_FILE", prepare["Env"])

    def test_unknown_service_provider_is_rejected(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.render(["--var", "service_provider=dns"])

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
