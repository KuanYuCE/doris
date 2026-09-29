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

# Nomad Pack variable file: pass it with `nomad-pack render|plan|run -f`.
# bootstrap_fe, discovery_fe_ips, fe_nodes and be_nodes have no defaults and
# must be set; every other value here overrides a default in variables.hcl.
# This is not Nomad agent configuration; see client.hcl for that.

job_name    = "doris"
namespace   = "default"
datacenters = ["dc1"]

# consul adds SQL readiness checks, Consul discovery and DNS; use "nomad"
# when the clients do not run a Consul agent.
service_provider = "consul"

vault_role         = "doris"
vault_secret_path  = "kv-data/data/doris-secret/bootstrap"
vault_password_key = "password"

# Independent configuration fragments; image defaults remain in effect.
fe_config = <<EOF
sys_log_level = INFO
EOF

be_config = <<EOF
sys_log_level = INFO
EOF

bootstrap_fe     = "10.0.0.11"
discovery_fe_ips = ["10.0.0.11", "10.0.0.12", "10.0.0.13"]

# hostname must match `nomad node status` Name, not an arbitrary Docker hostname.
# Set images per node so an upgrade changes only one FE group at a time.
fe_nodes = [
  { ip = "10.0.0.11", hostname = "doris-1", volume = "doris-fe", image = "apache/doris:fe-4.1.4" },
  { ip = "10.0.0.12", hostname = "doris-2", volume = "doris-fe", image = "apache/doris:fe-4.1.4" },
  { ip = "10.0.0.13", hostname = "doris-3", volume = "doris-fe", image = "apache/doris:fe-4.1.4" },
]

be_nodes = [
  { ip = "10.0.0.11", hostname = "doris-1", volume = "doris-be", image = "apache/doris:be-4.1.4" },
  { ip = "10.0.0.12", hostname = "doris-2", volume = "doris-be", image = "apache/doris:be-4.1.4" },
  { ip = "10.0.0.13", hostname = "doris-3", volume = "doris-be", image = "apache/doris:be-4.1.4" },
]
