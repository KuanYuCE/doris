#!/usr/bin/env python3
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

"""Create the initial Nomad Variable without printing its sensitive contents."""

import argparse
import getpass
import hashlib
import json
import subprocess


def credential_items(password):
    if not password or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise ValueError("Use a non-empty password without control characters")
    # MySQL PASSWORD() format: '*' + uppercase SHA1(SHA1(UTF8(password))).
    digest = hashlib.sha1(hashlib.sha1(password.encode("utf-8")).digest()).hexdigest()
    quoted = password.replace("\\", "\\\\").replace('"', '\\"')
    return {
        "my_cnf": f'[client]\npassword="{quoted}"\n',
        "root_password_hash": "*" + digest.upper(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", default="doris")
    parser.add_argument("--namespace", default="default")
    args = parser.parse_args()
    password = getpass.getpass("Initial Doris root password: ")
    if password != getpass.getpass("Confirm password: "):
        parser.error("Passwords differ")
    try:
        items = credential_items(password)
    except ValueError as exc:
        parser.error(str(exc))
    payload = {
        "Path": f"nomad/jobs/{args.job}",
        "Namespace": args.namespace,
        "Items": items,
    }
    # CAS=0 deliberately refuses to overwrite an existing credential variable.
    # Changing this variable would not rotate the password in existing metadata.
    result = subprocess.run(
        ["nomad", "var", "put", "-in=json", "-out=none", "-check-index=0",
         f"-namespace={args.namespace}", "-"],
        input=json.dumps(payload), text=True, capture_output=True,
    )
    if result.returncode:
        # Do not echo tool output: an error response could include variable data.
        parser.exit(1, "Could not create credentials; check Nomad connectivity, ACLs, and whether the variable already exists.\n")
    print(f"Created credentials at {payload['Path']} in namespace {args.namespace}.")


if __name__ == "__main__":
    main()
