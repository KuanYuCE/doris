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

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "credentials", Path(__file__).resolve().parents[1] / "scripts/credentials.py"
)
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


class CredentialsTest(unittest.TestCase):
    def test_doris_documented_password_vector(self):
        items = credentials.credential_items("root@123")
        self.assertEqual(items["root_password_hash"], "*A00C34073A26B40AB4307650BFB9309D6BFA6999")
        self.assertEqual(items["my_cnf"], '[client]\npassword="root@123"\n')

    def test_option_file_quotes_comments_and_backslash(self):
        items = credentials.credential_items('a"b\\c#d; e')
        self.assertEqual(items["my_cnf"], '[client]\npassword="a\\"b\\\\c#d; e"\n')

    def test_reject_empty_and_control_characters(self):
        for password in ("", "a\nb", "a\rb", "a\x00b", "a\tb"):
            with self.subTest(password=repr(password)):
                with self.assertRaises(ValueError):
                    credentials.credential_items(password)


if __name__ == "__main__":
    unittest.main()
