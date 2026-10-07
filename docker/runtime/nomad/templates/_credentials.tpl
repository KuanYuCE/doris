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

[[ define "credentials" ]]
[[ $root := .root ]]
[[ $key := var "vault_password_key" $root ]]
[[/* Field access, unlike index, honours error_on_missing_key: a missing key
     fails the template instead of rendering an empty password. */]]
[[ if not (regexMatch "^[A-Za-z_][A-Za-z0-9_]*$" $key) ]][[ fail "vault_password_key must be a template identifier ([A-Za-z_][A-Za-z0-9_]*)" ]][[ end ]]
vault {
  role         = [[ var "vault_role" $root | quote ]]
  env          = false
  disable_file = true
  change_mode  = "noop"
}
template {
  destination          = "secrets/my.cnf"
  perms                = "0600"
  uid                  = 0
  gid                  = 0
  error_on_missing_key = true
  once                 = true
  data                 = <<EOF
{{ with secret [[ var "vault_secret_path" $root | quote ]] }}
[client]
password="{{ .Data.data.[[ $key ]] | replaceAll "\\" "\\\\" | replaceAll "\"" "\\\"" | replaceAll "\n" "\\n" | replaceAll "\r" "\\r" | replaceAll "\t" "\\t" }}"
{{ end }}
EOF
}
[[ if and .prepare (eq .kind "fe") ]]
template {
  destination          = "secrets/root-password"
  perms                = "0600"
  uid                  = 0
  gid                  = 0
  error_on_missing_key = true
  once                 = true
  # Whitespace trimming preserves the exact password bytes for hashing.
  data = <<EOF
{{- with secret [[ var "vault_secret_path" $root | quote ]] -}}{{ .Data.data.[[ $key ]] }}{{- end -}}
EOF
}
[[ end ]]
[[ end ]]
