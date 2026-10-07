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

job [[ var "job_name" . | quote ]] {
  namespace   = [[ var "namespace" . | quote ]]
  datacenters = [[ var "datacenters" . | toJson ]]
  type        = "service"
  node_pool   = [[ var "node_pool" . | quote ]]

  spread {
    attribute = "${meta.dc}"
  }

  spread {
    attribute = "${meta.rack}"
  }
  constraint {
    attribute = "${meta.service}"
    value     = [[ var "meta_service" . | quote ]]
  }
  [[- if ne (var "meta_pool" .) "" ]]
  constraint {
    attribute = "${meta.pool}"
    value     = [[ var "meta_pool" . | quote ]]
  }
  [[- end ]]

  [[ range $kind, $nodes := dict "fe" $feNodes "be" $beNodes ]]
  [[ range $idx, $node := $nodes ]]
  [[ $overrides := var (printf "%s_config" $kind) $root ]]
  group [[ printf "%s-%s" $kind $node.hostname | quote ]] {
    count = 1
    constraint {
      attribute = "${node.unique.name}"
      value     = [[ $node.hostname | quote ]]
    }
    constraint {
      attribute = "${meta.doris_blocks}"
      operator  = "set_contains"
      value     = [[ $kind | quote ]]
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

    [[ if eq $kind "fe" ]]
    volume "meta" {
      type      = "host"
      source    = "[[ template "blockVolumeName" (list "fe" $node.block_index 0) ]]"
      read_only = false
    }
    volume "log" {
      type      = "host"
      source    = "[[ template "blockVolumeName" (list "fe" $node.block_index 1) ]]"
      read_only = false
    }
    [[ else ]]
    [[ range $d := until (var "be_disks_per_block" $root) ]]
    volume "storage[[ add1 $d ]]" {
      type      = "host"
      source    = "[[ template "blockVolumeName" (list "be" $node.block_index $d) ]]"
      read_only = false
    }
    [[ end ]]
    [[ end ]]

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
        image        = "[[ ternary (var "fe_image_repo" $root) (var "be_image_repo" $root) (eq $kind "fe") ]]:[[ $kind ]]-[[ template "versionOverride" (list (var (printf "%s_version_overrides" $kind) $root) (printf "%v" $idx) (var "doris_version" $root)) ]]"
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
        CONSUL_FE_FILE = "/local/consul-fe"
        [[ if eq $kind "be" ]]
        BE_DISK_COUNT = [[ var "be_disks_per_block" $root | toString | quote ]]
        [[ else ]]
        # Named in the bootstrap error and read by scripts/bootstrap-permit.sh.
        META_VOLUME = "[[ template "blockVolumeName" (list "fe" $node.block_index 0) ]]"
        NODE_NAME   = [[ $node.hostname | quote ]]
        [[ end ]]
      }
      [[ if eq $kind "fe" ]]
      # Prestart inspects ROLE/VERSION and consumes the bootstrap permit. It
      # never touches BE storage; init_be.sh checks that in the main task.
      volume_mount {
        volume      = "meta"
        destination = "/opt/apache-doris/fe/doris-meta"
      }
      [[ end ]]
      template {
        destination = "local/prestart.sh"
        perms       = "0644"
        once        = true
        data        = [[ fileContents (printf "%s/scripts/prestart.sh" $packPath) | replace "${" "$${" | replace "%{" "%%{" | toJson ]]
      }

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
        image        = "[[ ternary (var "fe_image_repo" $root) (var "be_image_repo" $root) (eq $kind "fe") ]]:[[ $kind ]]-[[ template "versionOverride" (list (var (printf "%s_version_overrides" $kind) $root) (printf "%v" $idx) (var "doris_version" $root)) ]]"
        network_mode = "host"
        # No command, args, or entrypoint override: use the original image.
        volumes = [
          "secrets/my.cnf:/root/.my.cnf:ro",
          [[ printf "../alloc/data/conf:/opt/apache-doris/%s/conf" $kind | quote ]],
        ]
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
      [[ if eq $kind "fe" ]]
      volume_mount {
        volume      = "meta"
        destination = "/opt/apache-doris/fe/doris-meta"
      }
      volume_mount {
        volume      = "log"
        destination = "/opt/apache-doris/fe/log"
      }
      [[ else ]]
      [[ range $d := until (var "be_disks_per_block" $root) ]]
      volume_mount {
        volume      = "storage[[ add1 $d ]]"
        destination = "/opt/apache-doris/be/storage/data[[ add1 $d ]]"
      }
      [[ end ]]
      [[ end ]]
      [[ template "credentials" (dict "root" $root "kind" $kind "prepare" false) ]]
      [[ $port := ternary "query" "heartbeat" (eq $kind "fe") ]]
      template {
        destination = "local/consul_ready.sh"
        perms       = "0644"
        once        = true
        data        = [[ fileContents (printf "%s/scripts/consul_ready.sh" $packPath) | replace "${" "$${" | replace "%{" "%%{" | toJson ]]
      }
      service {
        provider = "consul"
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
        # Authenticated membership readiness; also gates deployments.
        check {
          name     = "sql-ready"
          type     = "script"
          command  = "/bin/bash"
          args     = [[ concat (list "/local/consul_ready.sh" $kind $node.ip) (ternary (list) $seeds (eq $kind "fe")) | toJson ]]
          interval = "15s"
          timeout  = "10s"
        }
      }
      [[ if eq $kind "fe" ]]
      service {
        provider = "consul"
        name     = [[ printf "%s-fe-editlog" (var "job_name" $root) | quote ]]
        port     = "edit_log"
        address  = [[ $node.ip | quote ]]
        tags     = ["editlog", [[ printf "fe-id-%v" $idx | quote ]]]
        meta {
          node = [[ $node.hostname | quote ]]
        }
        check {
          type     = "tcp"
          port     = "edit_log"
          interval = "10s"
          timeout  = "3s"
        }
      }
      [[ end ]]
      service {
        provider = "consul"
        name     = [[ printf "%s-%s-flight" (var "job_name" $root) $kind | quote ]]
        port     = "arrow"
        address  = [[ $node.ip | quote ]]
        tags     = ["flight", "arrow-flight-sql", [[ printf "%s-id-%v" $kind $idx | quote ]]]
        meta {
          node = [[ $node.hostname | quote ]]
        }
        check {
          type     = "tcp"
          port     = "arrow"
          interval = "10s"
          timeout  = "3s"
        }
      }
      [[ if eq $kind "fe" ]]
      service {
        provider = "consul"
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
