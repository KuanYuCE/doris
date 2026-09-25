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

[[ $root := . ]]
[[ $feNodes := var "fe_nodes" . ]]
[[ $beNodes := var "be_nodes" . ]]
[[ $bootstrap := var "bootstrap_fe" . ]]
[[ $ips := list ]]
[[ $seen := dict ]]
[[ range $feNodes ]]
[[ if hasKey $seen .ip ]][[ fail "Duplicate FE IP" ]][[ end ]]
[[ $_ := set $seen .ip true ]]
[[ $ips = append $ips .ip ]]
[[ end ]]
[[ if not (has $bootstrap $ips) ]][[ fail "bootstrap_fe must occur in fe_nodes" ]][[ end ]]
[[ $seeds := var "discovery_fe_ips" . ]]
[[ if not $seeds ]][[ fail "discovery_fe_ips must not be empty" ]][[ end ]]

job [[ var "job_name" . | quote ]] {
  namespace   = [[ var "namespace" . | quote ]]
  datacenters = [[ var "datacenters" . | toJson ]]
  type        = "service"

  [[ range $kind, $nodes := dict "fe" $feNodes "be" $beNodes ]]
  [[ range $node := $nodes ]]
  group [[ printf "%s-%s" $kind $node.hostname | quote ]] {
    count = 1
    constraint {
      attribute = "${node.unique.name}"
      value     = [[ $node.hostname | quote ]]
    }

    # A new allocation reruns prestart. A main-task-only restart would reuse
    # the completed prestart task and its possibly stale endpoint.env.
    restart {
      attempts = 0
      mode     = "fail"
    }
    reschedule {
      delay          = "15s"
      delay_function = "exponential"
      max_delay      = "2m"
      unlimited      = true
    }
    update {
      max_parallel      = 1
      min_healthy_time  = "30s"
      healthy_deadline  = "15m"
      progress_deadline = "30m"
      auto_revert       = false
    }

    volume "data" {
      type      = "host"
      source    = [[ $node.volume | quote ]]
      read_only = false
    }
    network {
      mode = "host"
      [[ if eq $kind "fe" ]]
      port "http" { static = 8030 }
      port "edit_log" { static = 9010 }
      port "query" { static = 9030 }
      port "rpc" { static = 9020 }
      port "arrow" { static = 8070 }
      [[ else ]]
      port "http" { static = 8040 }
      port "heartbeat" { static = 9050 }
      port "be" { static = 9060 }
      port "brpc" { static = 8060 }
      port "arrow" { static = 8050 }
      [[ end ]]
    }

    task "prepare" {
      lifecycle {
        hook    = "prestart"
        sidecar = false
      }
      driver = "docker"
      user   = "0"
      config {
        image        = [[ $node.image | quote ]]
        network_mode = "host"
        # Only this short-lived helper overrides the image entrypoint.
        entrypoint = ["/bin/bash", "/local/prestart.sh"]
      }
      env {
        NODE_KIND             = [[ $kind | quote ]]
        NODE_IP               = [[ $node.ip | quote ]]
        BOOTSTRAP_IP          = [[ $bootstrap | quote ]]
        FE_CANDIDATES         = [[ join " " $seeds | quote ]]
        DISCOVERY_TIMEOUT     = [[ var "discovery_timeout" $root | toString | quote ]]
        CONFIG_OVERRIDES_FILE = [[ printf "/local/%s-overrides.conf" $kind | quote ]]
        [[ if and (eq $kind "fe") (eq (var "credential_source" $root) "vault") ]]
        ROOT_PASSWORD_FILE = "/secrets/root-password"
        [[ end ]]
      }
      volume_mount {
        volume      = "data"
        destination = [[ printf "/opt/apache-doris/%s/%s" $kind (ternary "doris-meta" "storage" (eq $kind "fe")) | quote ]]
      }
      template {
        destination = "local/prestart.sh"
        perms       = "0644"
        once        = true
        data        = [[ fileContents (printf "%s/scripts/prestart.sh" (meta "pack.path" $root)) | replace "${" "$${" | replace "%{" "%%{" | toJson ]]
      }
      [[ if eq $kind "fe" ]]
      [[ template "fe-config" $root ]]
      [[ else ]]
      [[ template "be-config" $root ]]
      [[ end ]]
      [[ template "credentials" (dict "root" $root "kind" $kind "prepare" true) ]]
      resources {
        cpu    = 100
        memory = 256
      }
    }

    task "doris" {
      driver         = "docker"
      user           = "0"
      kill_timeout   = "2m"
      shutdown_delay = "10s"
      config {
        image        = [[ $node.image | quote ]]
        network_mode = "host"
        # No command, args, or entrypoint override: use the original image.
        volumes = [
          "secrets/my.cnf:/root/.my.cnf:ro",
          [[ printf "../alloc/data/conf:/opt/apache-doris/%s/conf" $kind | quote ]],
        ]
        ulimit {
          nofile = "65536:65536"
        }
      }
      env {
        HOME     = "/root"
        BASH_ENV = "/alloc/data/endpoint.env"
        [[ if eq $kind "fe" ]]
        FE_CURRENT_IP   = [[ $node.ip | quote ]]
        FE_CURRENT_PORT = "9010"
        [[ else ]]
        BE_IP   = [[ $node.ip | quote ]]
        BE_PORT = "9050"
        [[ end ]]
      }
      volume_mount {
        volume      = "data"
        destination = [[ printf "/opt/apache-doris/%s/%s" $kind (ternary "doris-meta" "storage" (eq $kind "fe")) | quote ]]
      }
      [[ template "credentials" (dict "root" $root "kind" $kind "prepare" false) ]]
      service {
        provider = "nomad"
        name     = [[ printf "%s-%s" (var "job_name" $root) $kind | quote ]]
        port     = [[ ternary "query" "heartbeat" (eq $kind "fe") | quote ]]
        address  = [[ $node.ip | quote ]]
        check {
          name     = "tcp-listener"
          type     = "tcp"
          interval = "10s"
          timeout  = "2s"
        }
      }
      resources {
        cpu    = [[ var (printf "%s_cpu" $kind) $root ]]
        memory = [[ var (printf "%s_memory" $kind) $root ]]
      }
    }
  }
  [[ end ]]
  [[ end ]]
}
