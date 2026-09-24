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
template {
  destination          = "secrets/my.cnf"
  perms                = "0600"
  uid                  = 0
  gid                  = 0
  error_on_missing_key = true
  once                 = true
  data                 = <<EOF
{{ with nomadVar "nomad/jobs/[[ var "job_name" . ]]" }}{{ .my_cnf }}{{ end }}
EOF
}
template {
  destination          = "secrets/root-password-hash"
  perms                = "0600"
  uid                  = 0
  gid                  = 0
  error_on_missing_key = true
  once                 = true
  data                 = <<EOF
{{ with nomadVar "nomad/jobs/[[ var "job_name" . ]]" }}{{ .root_password_hash }}{{ end }}
EOF
}
[[ end ]]
