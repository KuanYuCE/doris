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

# Shared by the bastion operator tools (bootstrap-permit.sh, wipe-volumes.sh).
# Sourced, not executed. The caller sets OPS_NAME before sourcing.
#
# The tools take the same arguments as the `nomad-pack run` they accompany,
# ending with the pack name (or path), e.g.
#   --registry=doris_packs --ref=pack-template --address=... --ca-cert=...
#   --namespace=doris --var-file=cluster.hcl doris
# --registry, --ref, --var-file (-f) and --var go to `nomad-pack render`, in
# the given order; the connection flags go to the nomad CLI. Both -flag and
# --flag spellings work, always as flag=value. Connection settings may also
# come from the usual NOMAD_* environment variables (NOMAD_ADDR, NOMAD_CACERT,
# NOMAD_CLIENT_CERT, NOMAD_CLIENT_KEY, NOMAD_TLS_SERVER_NAME, NOMAD_NAMESPACE,
# NOMAD_REGION). One endpoint (normally a server) is enough. The token is
# accepted only as NOMAD_TOKEN so it never appears in argv.
#
# Remote work runs as `ssh <node IP> sudo -n bash -s`: other SSH settings
# belong in ~/.ssh/config, and the remote account needs passwordless sudo.

fail() { echo "$OPS_NAME: $*" >&2; exit 1; }

# Values are spliced into Go templates; only allow plain Nomad names.
check_name() {
    [[ $2 =~ ^[A-Za-z0-9._-]+$ ]] || fail "Unexpected $1: $2"
}

NOMAD_FLAGS=()
RENDER_ARGS=()
NAMESPACE_FLAGS=()
# The pack name or path (the positional argument); without one, the pack this
# script ships in (scripts/..).
PACK=""
DEFAULT_PACK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Consumes one shared option. Returns 1 when the argument is not one of them.
parse_common_option() {
    # nomad and nomad-pack accept both -flag and --flag.
    local opt=$1
    [[ $opt != --* ]] || opt=${opt#-}
    case $opt in
        -address=?* | -ca-cert=?* | -client-cert=?* | -client-key=?* | \
            -tls-server-name=?* | -region=?*)
            NOMAD_FLAGS+=("$opt") ;;
        # Not forwarded as is: every candidate namespace is checked explicitly.
        -namespace=?*) NAMESPACE_FLAGS+=("${opt#-namespace=}") ;;
        -token | -token=*) fail "Pass the token as NOMAD_TOKEN, not on the command line" ;;
        -var-file=?* | -f=?*) RENDER_ARGS+=("--var-file=${opt#*=}") ;;
        -var=?* | -registry=?* | -ref=?*) RENDER_ARGS+=("-$opt") ;;
        *) return 1 ;;
    esac
}

# Fails unless every TLS file the nomad CLI will read exists. As in nomad, a
# flag wins over its environment variable; relative paths resolve against the
# current directory.
check_tls_files() {
    local flag env file source
    for flag in ca-cert client-cert client-key; do
        case $flag in
            ca-cert) env=NOMAD_CACERT ;;
            client-cert) env=NOMAD_CLIENT_CERT ;;
            client-key) env=NOMAD_CLIENT_KEY ;;
        esac
        file=${!env:-}
        source=$env
        local arg
        for arg in "${NOMAD_FLAGS[@]}"; do
            if [[ $arg == -$flag=* ]]; then
                file=${arg#-"$flag"=}
                source=--$flag
            fi
        done
        [[ -z $file || -r $file ]] ||
            fail "Cannot read $file (from $source) in $PWD; fix the path or unset $env"
    done
}

# Sets the pack from the positional argument, which may be given only once.
set_pack() {
    [[ -z $PACK ]] || fail "More than one pack given: $PACK and $1"
    PACK=$1
}

# True when the render gets any variables (a var file or --var).
has_variables() {
    local arg
    for arg in "${RENDER_ARGS[@]}"; do
        [[ $arg != --var-file=* && $arg != --var=* ]] || return 0
    done
    return 1
}

# Renders the pack locally exactly as `nomad-pack run` would with the same
# pack, registry, ref, variables, order and working directory, and sets
# RENDERED_JOB. Must not run in a command substitution: the EXIT trap would
# delete the render early.
render_job() {
    local pack=${PACK:-$DEFAULT_PACK}
    RENDER_DIR=$(mktemp -d)
    trap 'rm -rf "$RENDER_DIR"' EXIT
    # Rendering never contacts Nomad, but nomad-pack still builds a client from
    # NOMAD_* and fails on, say, a stale NOMAD_CACERT that a --ca-cert flag
    # overrides for the nomad CLI. Render without them (the token included).
    # nomad-pack reports template and variable errors on stdout, so keep its
    # output and show it when the render fails.
    local out
    if ! out=$(env -u NOMAD_ADDR -u NOMAD_TOKEN -u NOMAD_CACERT -u NOMAD_CAPATH \
        -u NOMAD_CLIENT_CERT -u NOMAD_CLIENT_KEY -u NOMAD_TLS_SERVER_NAME \
        -u NOMAD_SKIP_VERIFY -u NOMAD_NAMESPACE -u NOMAD_REGION \
        nomad-pack render "$pack" "${RENDER_ARGS[@]}" --to-dir "$RENDER_DIR" --auto-approve 2>&1); then
        echo "$out" >&2
        fail "nomad-pack render failed for $pack (see the output above)"
    fi
    local -a jobs
    mapfile -t jobs < <(find "$RENDER_DIR" -name '*.nomad' -type f)
    [[ ${#jobs[@]} -eq 1 ]] || fail "Expected one rendered job, found ${#jobs[@]}"
    RENDERED_JOB=${jobs[0]}
}

# Job ID of the rendered job (templates/doris.nomad.tpl writes `job "<id>" {`).
rendered_job_id() {
    awk 'match($0, /^job "[^"]+"/) { print substr($0, 6, RLENGTH - 6); exit }' "$RENDERED_JOB"
}

# Namespace written in the rendered job (the pack's `namespace` variable).
rendered_namespace() {
    awk 'match($0, /^  namespace[[:space:]]*=[[:space:]]*"[^"]+"/) {
        ns = substr($0, RSTART, RLENGTH); sub(/^[^"]*"/, "", ns); sub(/"$/, "", ns); print ns; exit
    }' "$RENDERED_JOB"
}

# Prints every namespace the job may live in, once each: the rendered job's,
# plus --namespace and NOMAD_NAMESPACE, which `nomad-pack run` may apply
# instead. Checking all of them never misses a running job.
job_namespaces() {
    local -A seen=()
    local ns
    for ns in "$(rendered_namespace)" "${NAMESPACE_FLAGS[@]}" ${NOMAD_NAMESPACE:+"$NOMAD_NAMESPACE"}; do
        check_name "namespace" "$ns"
        [[ -z ${seen[$ns]:-} ]] || continue
        seen[$ns]=1
        echo "$ns"
    done
}

# Fails unless the job is absent, or stopped with no live allocations, in every
# namespace it may live in.
require_job_stopped_everywhere() {
    local job=$1 namespaces ns
    # Plain assignment, so a rejected namespace stops the script.
    namespaces=$(job_namespaces)
    while read -r ns; do
        require_job_stopped "$job" "$ns"
    done <<< "$namespaces"
}

# Fails unless the job is absent, or stopped with no running or pending
# allocations, so nothing is running on or about to start on its volumes.
require_job_stopped() {
    local job=$1 namespace=$2 out
    if ! out=$(nomad job inspect "${NOMAD_FLAGS[@]}" -namespace="$namespace" -t '{{.ID}} {{.Stop}}' "$job" 2>&1); then
        [[ $out == *"No job(s) with prefix or ID"* ]] || fail "Cannot check job $job: $out"
        echo "job:    $job (namespace $namespace) is not registered"
        return
    fi
    local id stop
    read -r id stop <<< "$out"
    if [[ $id != "$job" ]]; then
        # Prefix matching found another job, so this one is not registered.
        echo "job:    $job (namespace $namespace) is not registered"
        return
    fi
    [[ $stop == true ]] ||
        fail "Job $job is still running; stop it first: nomad job stop -namespace=$namespace $job"
    local statuses
    statuses=$(nomad job allocs "${NOMAD_FLAGS[@]}" -namespace="$namespace" -t '{{range .}}{{.ClientStatus}}{{"\n"}}{{end}}' "$job") ||
        fail "Cannot list allocations of job $job"
    if grep -Exq 'running|pending' <<< "$statuses"; then
        fail "Job $job still has running or pending allocations; wait until they stop"
    fi
    echo "job:    $job (namespace $namespace) is stopped"
}

# Prints the ID of the single Nomad node with this name.
node_id_of() {
    local node=$1 ids
    check_name "node name" "$node"
    ids=$(nomad node status "${NOMAD_FLAGS[@]}" -t "{{range .}}{{if eq .Name \"$node\"}}{{.ID}}{{\"\\n\"}}{{end}}{{end}}") ||
        fail "Cannot list Nomad nodes"
    local -a node_ids
    mapfile -t node_ids <<< "$ids"
    [[ ${#node_ids[@]} -eq 1 && -n ${node_ids[0]} ]] ||
        fail "Expected exactly one Nomad node named $node, found: ${ids:-none}"
    check_name "node ID" "${node_ids[0]}"
    echo "${node_ids[0]}"
}

# Prints "<advertised host> <path>" of a host volume on a node, given the node
# name (for messages) and its ID. The client
# reports both static and dynamic host volumes with their path, and its
# advertised HTTP address identifies the host to connect to.
volume_on_node() {
    local node=$1 node_id=$2 volume=$3 info http_addr path
    check_name "volume name" "$volume"
    info=$(nomad node status "${NOMAD_FLAGS[@]}" -t "{{.HTTPAddr}} {{with index .HostVolumes \"$volume\"}}{{.Path}}{{end}}" "$node_id") ||
        fail "Cannot read node $node ($node_id)"
    read -r http_addr path <<< "$info"
    [[ -n $http_addr ]] || fail "Node $node ($node_id) reports no HTTP address"
    if [[ -z $path ]]; then
        # A dynamic host volume is listed only once it exists and is ready, so
        # show what the node does have: a name or block_index mismatch is obvious.
        local have
        have=$(nomad node status "${NOMAD_FLAGS[@]}" -t '{{range $name, $v := .HostVolumes}}{{$name}} {{end}}' "$node_id") ||
            fail "Cannot read node $node ($node_id)"
        have=${have% }
        fail "Node $node ($node_id) has no host volume $volume; it has: ${have:-none}. Check the volume name and block_index, and that the volume is ready (nomad volume status -type host)"
    fi
    local host=${http_addr%:*}
    host=${host#[}
    host=${host%]}
    echo "$host $path"
}

# Remote side: several nodes may lay out volumes identically, so a matching
# path alone does not prove this is the intended node; its address must be ours.
remote_owns_address() {
    ip -o addr show | awk '{ sub("/.*", "", $4); print $4 }' | grep -Fxq -- "$1" ||
        fail "$(hostname) does not own $1; the SSH target is the wrong machine"
}

# Runs a function of this file's caller on <target> as root. The function and
# its helpers travel on stdin; the values are quoted positional arguments,
# never spliced into the script text.
run_remote() {
    local target=$1 func=$2
    shift 2
    [[ $target != -* ]] || fail "Unexpected SSH host: $target"
    { declare -f fail remote_owns_address "$func"; echo "OPS_NAME=$OPS_NAME"; echo "$func \"\$@\""; } |
        ssh -- "$target" "sudo -n bash -s -- $(printf '%q ' "$@")" ||
        fail "Remote step on $target failed; see the message above (the remote account needs passwordless sudo)"
}
