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

"""Render the pack's Vault templates using an isolated real Vault Agent.

No existing Vault address, token or namespace is used. All secrets are synthetic.
"""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import test_pack


@unittest.skipUnless(shutil.which("vault"), "Vault CLI required for local template integration")
class VaultTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name)
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith("VAULT_")}
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.address = f"http://127.0.0.1:{port}"
        cls.token = "doris-local-template-test-only"
        log = (cls.base / "server.log").open("w")
        cls.addClassCleanup(log.close)
        cls.server = subprocess.Popen(
            ["vault", "server", "-dev", "-dev-no-store-token",
             f"-dev-listen-address=127.0.0.1:{port}", f"-dev-root-token-id={cls.token}"],
            env=cls.env, stdout=log, stderr=subprocess.STDOUT,
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 15
        while True:
            try:
                cls.request("GET", "sys/health")
                break
            except (urllib.error.URLError, TimeoutError):
                if cls.server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Local test Vault failed to start: " +
                                       (cls.base / "server.log").read_text())
                time.sleep(0.1)
        cls.request("POST", "sys/mounts/kv-data", {"type": "kv", "options": {"version": "2"}})
        job = test_pack.PackTest().render()
        group = next(g for g in job["TaskGroups"] if g["Name"].startswith("fe-"))
        prepare = next(t for t in group["Tasks"] if t["Name"] == "prepare")
        cls.templates = {t["DestPath"]: t["EmbeddedTmpl"] for t in prepare["Templates"]}

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait()

    @classmethod
    def request(cls, method, path, payload=None):
        request = urllib.request.Request(
            cls.address + "/v1/" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"X-Vault-Token": cls.token, "Content-Type": "application/json"},
            method=method,
        )
        # Ignore proxy environment variables: the test only talks to loopback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            return response.read()

    def render_password(self, password):
        self.request("POST", "kv-data/data/doris-secret/bootstrap", {"data": {"password": password}})
        with tempfile.TemporaryDirectory(dir=self.base) as tmp:
            path = Path(tmp)
            (path / "token").write_text(self.token)
            config = (
                'exit_after_auth = true\n'
                f'vault {{ address = "{self.address}" }}\n'
                'auto_auth {\n  method "token_file" {\n'
                f'    config = {{ token_file_path = "{path / "token"}" }}\n'
                '  }\n}\n'
                'template_config { exit_on_retry_failure = true }\n'
            )
            for name in ("my.cnf", "root-password"):
                source = path / (name + ".tpl")
                source.write_text(self.templates["secrets/" + name])
                config += (
                    'template {\n'
                    f'  source = "{source}"\n'
                    f'  destination = "{path / name}"\n'
                    '  error_on_missing_key = true\n'
                    '  perms = "0600"\n}\n'
                )
            (path / "agent.hcl").write_text(config)
            result = subprocess.run(
                ["vault", "agent", f"-config={path / 'agent.hcl'}"],
                env=self.env, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((path / "root-password").read_bytes(), password.encode())
            self.assertEqual((path / "my.cnf").stat().st_mode & 0o777, 0o600)
            return (path / "my.cnf").read_text().strip()

    def test_kv_v2_password_rendering(self):
        self.assertEqual(self.render_password("root@123"), '[client]\npassword="root@123"')

    def test_password_escaping_and_raw_bytes(self):
        self.assertEqual(
            self.render_password(' leading"\\#;\tline\n尾\r\n'),
            '[client]\npassword=" leading\\"\\\\#;\\tline\\n尾\\r\\n"',
        )


if __name__ == "__main__":
    unittest.main()
