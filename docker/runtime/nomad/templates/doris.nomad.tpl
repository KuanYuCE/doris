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
[[/* meta "pack.path" only exists from Nomad Pack 0.4.2. The root variable
     file's absolute path is available in 0.4.1 and later. */]]
[[ $packPath := dir (index . "_self").Pack.RootVariableFile.Path ]]
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
[[ $provider := var "service_provider" . ]]
[[ if not (has $provider (list "nomad" "consul")) ]][[ fail "service_provider must be nomad or consul" ]][[ end ]]

job [[ var "job_name" . | quote ]] {
  namespace   = [[ var "namespace" . | quote ]]
  datacenters = [[ var "datacenters" . | toJson ]]
  type        = "service"

  [[ range $kind, $nodes := dict "fe" $feNodes "be" $beNodes ]]
  [[ range $node := $nodes ]]
  [[ $overrides := var (printf "%s_config" $kind) $root ]]
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
        [[ if $overrides ]]
        # Nomad rejects an empty template, so an empty fragment is omitted.
        CONFIG_OVERRIDES_FILE = [[ printf "/local/%s-overrides.conf" $kind | quote ]]
        [[ end ]]
        [[ if eq $provider "consul" ]]
        CONSUL_FE_FILE = "/local/consul-fe"
        [[ end ]]
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
        data        = [[ fileContents (printf "%s/scripts/prestart.sh" $packPath) | replace "${" "$${" | replace "%{" "%%{" | toJson ]]
      }
      [[ if eq $provider "consul" ]]
      # Healthy (SQL-ready) FEs at task start. An empty list is valid: the
      # static seeds still cover bootstrap and a new catalog.
      template {
        destination = "local/consul-fe"
        once        = true
        data        = <<EOF
{{ range service [[ printf "%s-fe" (var "job_name" $root) | quote ]] }}{{ .Address }}
{{ end }}
EOF
      }
      [[ end ]]
      [[ if and $overrides (eq $kind "fe") ]]
      [[ template "fe-config" $root ]]
      [[ else if $overrides ]]
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
      [[ $port := ternary "query" "heartbeat" (eq $kind "fe") ]]
      [[ if eq $provider "consul" ]]
      template {
        destination = "local/ready.sh"
        perms       = "0644"
        once        = true
        data        = [[ fileContents (printf "%s/scripts/ready.sh" $packPath) | replace "${" "$${" | replace "%{" "%%{" | toJson ]]
      }
      [[ end ]]
      service {
        provider = [[ $provider | quote ]]
        name     = [[ printf "%s-%s" (var "job_name" $root) $kind | quote ]]
        port     = [[ $port | quote ]]
        address  = [[ $node.ip | quote ]]
        tags     = [[ list $kind $port | toJson ]]
        meta {
          node = [[ $node.hostname | quote ]]
        }
        check {
          name     = "tcp-listener"
          type     = "tcp"
          interval = "10s"
          timeout  = "2s"
        }
        [[ if eq $provider "consul" ]]
        # Authenticated membership readiness; also gates deployments.
        check {
          name     = "sql-ready"
          type     = "script"
          command  = "/bin/bash"
          args     = [[ concat (list "/local/ready.sh" $kind $node.ip) (ternary (list) $seeds (eq $kind "fe")) | toJson ]]
          interval = "15s"
          timeout  = "10s"
        }
        [[ end ]]
      }
      [[ if eq $kind "fe" ]]
      service {
        provider = [[ $provider | quote ]]
        name     = [[ printf "%s-fe-http" (var "job_name" $root) | quote ]]
        port     = "http"
        address  = [[ $node.ip | quote ]]
        tags     = ["fe", "http"]
        meta {
          node = [[ $node.hostname | quote ]]
        }
        # Public endpoint; returns 503 until the FE has finished starting.
        check {
          name     = "api-health"
          type     = "http"
          path     = "/api/health"
          interval = "10s"
          timeout  = "2s"
        }
      }
      [[ end ]]
      resources {
        cpu    = [[ var (printf "%s_cpu" $kind) $root ]]
        memory = [[ var (printf "%s_memory" $kind) $root ]]
      }
    }
  }
  [[ end ]]
  [[ end ]]
}
