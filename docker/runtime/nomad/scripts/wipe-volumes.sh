#!/usr/bin/env bash
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

# Operator helper, run from a bastion host outside Nomad tasks. DESTROYS ALL
# DORIS DATA of a job so the cluster can be bootstrapped again: empties every
# host volume (FE meta and log, every BE disk) of every group, keeping the
# directories themselves.
#
#   wipe-volumes.sh --dry-run <nomad-pack run arguments>
#   wipe-volumes.sh --confirm=<job> <nomad-pack run arguments>
# for example
#   wipe-volumes.sh --dry-run --registry=doris_packs --ref=pack-template \
#       --address=https://nomad:4646 --ca-cert=ca.pem --namespace=doris \
#       --var-file=cluster.hcl doris
#
# Groups and volumes come from a local `nomad-pack render` with the same
# arguments, order and working directory as `nomad-pack run`; paths come from
# the Nomad node API. The job must be absent, or stopped with no running or
# pending allocations, in every namespace it may live in (the rendered one,
# --namespace and NOMAD_NAMESPACE); every volume must resolve before anything is
# deleted; and --confirm must name the job.
# See ops-common.sh for connection and SSH settings.

set -euo pipefail

OPS_NAME=wipe-volumes
# shellcheck source=ops-common.sh
source "$(dirname "${BASH_SOURCE[0]}")/ops-common.sh"

usage() { fail "usage: $0 (--dry-run | --confirm=<job>) <the nomad-pack run arguments: [--registry=R] [--ref=REF] [nomad flags] --var-file=FILE... [--var=K=V...] [PACK]>"; }

# Runs on one node as root: its advertised address, then its volume paths.
# Every path is checked before any is emptied.
remote_wipe() {
    local host=$1 path resolved
    shift
    remote_owns_address "$host"
    local -a resolved_paths=()
    for path; do
        [[ $path == /* ]] || fail "Not an absolute path: $path"
        resolved=$(realpath -e -- "$path") || fail "Host volume path $path does not exist on $(hostname)"
        [[ -d $resolved ]] || fail "$resolved is not a directory on $(hostname)"
        # Refuse / and top-level directories such as /data or /srv.
        [[ $resolved =~ ^/[^/]+/.+ ]] || fail "Refusing to empty $resolved on $(hostname)"
        resolved_paths+=("$resolved")
    done
    for path in "${resolved_paths[@]}"; do
        # Keep the directory (it may be a mount point) and lost+found; do not
        # descend into other file systems mounted below it.
        find "$path" -mindepth 1 -maxdepth 1 ! -name lost+found \
            -exec rm -rf --one-file-system -- {} +
        echo "Emptied on $(hostname): $path"
    done
}

# Prints "<group> <node> <volume>" for every host volume of every group. The
# blocks are produced by templates/doris.nomad.tpl: a node.unique.name
# constraint followed by its value, and one `source = "..."` per volume.
volumes_from_render() {
    awk '
        match($0, /^  group "[^"]+"/) { group = substr($0, RSTART + 9, RLENGTH - 10); node = ""; next }
        /attribute[[:space:]]*=[[:space:]]*"\$\{node\.unique\.name\}"/ { want_node = 1; next }
        want_node && match($0, /value[[:space:]]*=[[:space:]]*"[^"]+"/) {
            node = substr($0, RSTART, RLENGTH); sub(/^[^"]*"/, "", node); sub(/"$/, "", node)
            want_node = 0; next
        }
        /^    volume "[^"]+"/ { in_volume = 1; next }
        in_volume && match($0, /source[[:space:]]*=[[:space:]]*"[^"]+"/) {
            volume = substr($0, RSTART, RLENGTH); sub(/^[^"]*"/, "", volume); sub(/"$/, "", volume)
            print group, node, volume
            in_volume = 0
        }
    ' "$RENDERED_JOB"
}

main() {
    local dry_run=false confirm=""
    local -a orig=("$@")
    while [[ $# -gt 0 ]]; do
        if ! parse_common_option "$1"; then
            case $1 in
                --dry-run) dry_run=true ;;
                --confirm=?*) confirm=${1#--confirm=} ;;
                -*) usage ;;
                *) set_pack "$1" ;;
            esac
        fi
        shift
    done
    has_variables || usage
    [[ $dry_run == true || -n $confirm ]] || usage

    check_tls_files
    render_job
    local job
    job=$(rendered_job_id)
    check_name "job" "$job"
    [[ $dry_run == true || $confirm == "$job" ]] ||
        fail "--confirm=$confirm does not match job $job"
    require_job_stopped_everywhere "$job"

    local rows
    rows=$(volumes_from_render)
    [[ -n $rows ]] || fail "No host volumes found in the render"

    # Resolve everything first: nothing is deleted unless every volume resolves.
    local -A host_of=() paths_of=() id_of=()
    local -a nodes=()
    local group node volume info host path
    while read -r group node volume; do
        check_name "node name" "$node"
        if [[ -z ${id_of[$node]:-} ]]; then
            id_of[$node]=$(node_id_of "$node")
            nodes+=("$node")
        fi
        info=$(volume_on_node "$node" "${id_of[$node]}" "$volume")
        read -r host path <<< "$info"
        [[ -z ${host_of[$node]:-} || ${host_of[$node]} == "$host" ]] ||
            fail "Node $node reports two addresses: ${host_of[$node]} and $host"
        host_of[$node]=$host
        paths_of[$node]+="$path"$'\n'
        printf '%-14s %-10s %-16s %s:%s\n' "$group" "$node" "$volume" "$host" "$path"
    done <<< "$rows"

    if [[ $dry_run == true ]]; then
        local -a rerun=()
        local arg
        for arg in "${orig[@]}"; do [[ $arg == --dry-run ]] || rerun+=("$arg"); done
        echo "Dry run, nothing deleted. To empty ALL of the volumes above:"
        echo "  $0 --confirm=$job$(printf ' %q' "${rerun[@]}")"
        return
    fi

    local -a node_paths
    for node in "${nodes[@]}"; do
        mapfile -t node_paths <<< "${paths_of[$node]%$'\n'}"
        run_remote "${host_of[$node]}" remote_wipe "${host_of[$node]}" "${node_paths[@]}"
    done
    echo "All volumes of job $job are empty. Next: bootstrap-permit.sh, then nomad-pack run."
}

main "$@"
