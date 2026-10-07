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

# Nomad client agent configuration, not a pack variable file.
# Merge into each designated Nomad client's configuration. The pack's
# constraints select clients by node_pool and meta, and its groups mount host
# volumes named doris-<fe|be>-b<block_index>-d<disk> (blockVolumeName in
# templates/_helpers.tpl). A dynamic host volume plugin can create the volumes
# instead; the names must match either way. Create each path on the correct
# persistent disk before starting the client.
client {
  node_pool = "doris"

  meta {
    service      = "doris"
    pool         = "shared"
    # Roles this client may run, matched with set_contains.
    doris_blocks = "fe,be"
    # Spread attributes; each group is pinned to one node, so they only label.
    dc   = "dc1"
    rack = "r1"
  }

  # FE block 0: d0 = doris-meta, d1 = log.
  host_volume "doris-fe-b0-d0" {
    path      = "/srv/doris/fe-b0/meta"
    read_only = false
  }
  host_volume "doris-fe-b0-d1" {
    path      = "/srv/doris/fe-b0/log"
    read_only = false
  }

  # BE block 0: one volume per disk, d0 .. be_disks_per_block-1.
  host_volume "doris-be-b0-d0" {
    path      = "/data1/doris/be-b0"
    read_only = false
  }
  host_volume "doris-be-b0-d1" {
    path      = "/data2/doris/be-b0"
    read_only = false
  }
}
