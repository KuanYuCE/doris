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

set -euo pipefail
umask 077

DORIS_HOME=${DORIS_HOME:-/opt/apache-doris}
ALLOC_DATA=${ALLOC_DATA:-/alloc/data}
DORIS_MY_CNF=${DORIS_MY_CNF:-/secrets/my.cnf}
ROOT_PASSWORD_FILE=${ROOT_PASSWORD_FILE:-/secrets/root-password}
CONFIG_OVERRIDES_FILE=${CONFIG_OVERRIDES_FILE:-}
CONSUL_FE_FILE=${CONSUL_FE_FILE:-}
DISCOVERY_TIMEOUT=${DISCOVERY_TIMEOUT:-300}
EXISTING_FE_DISCOVERY_TIMEOUT=${EXISTING_FE_DISCOVERY_TIMEOUT:-30}
MAX_CLOCK_SKEW_SECONDS=${MAX_CLOCK_SKEW_SECONDS:-4}
POLL_INTERVAL=${POLL_INTERVAL:-2}
BE_DISK_COUNT=${BE_DISK_COUNT:-}
META_VOLUME=${META_VOLUME:-}
NODE_NAME=${NODE_NAME:-}
fail() { echo "prestart: $*" >&2; exit 1; }

validate_ip() {
    local ip=$1 octet
    [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "Invalid IPv4 address"
    local -a octets
    IFS=. read -r -a octets <<< "$ip"
    for octet in "${octets[@]}"; do
        [[ ${#octet} -le 3 ]] && ((10#$octet <= 255)) || fail "Invalid IPv4 octet"
    done
}

sql() {
    # Bound each query as well as the overall discovery loop. Credentials never
    # appear in argv. Keep the option-file argument first.
    timeout 10 mysql --defaults-file="$DORIS_MY_CNF" --connect-timeout=2 \
        --batch --raw -uroot -P9030 -h "$1" -e "$2" 2>/dev/null
}

master_from() {
    awk -F '\t' '
        NR == 1 { for (i=1; i<=NF; i++) c[$i]=i; next }
        c["Host"] && c["IsMaster"] && c["Alive"] && c["EditLogPort"] &&
        c["Role"] && $(c["IsMaster"]) == "true" && $(c["Alive"]) == "true" &&
        $(c["Role"]) == "FOLLOWER" && $(c["EditLogPort"]) == "9010" {
            print $(c["Host"]); exit
        }'
}

lists_self() {
    # Succeeds when SHOW FRONTENDS (stdin) has this node's own row.
    awk -F '\t' -v host="$NODE_IP" '
        NR == 1 { for (i=1; i<=NF; i++) c[$i]=i; next }
        c["Host"] && c["EditLogPort"] && $(c["Host"]) == host && $(c["EditLogPort"]) == "9010" { found=1 }
        END { exit !found }'
}

# Finds the elected master within $1 seconds, re-checked from the master's own
# view. Sets master, master_rows and saw_master; fails on timeout.
discover_master() {
    local deadline=$((SECONDS + $1)) candidate rows verified
    master="" master_rows="" saw_master=false
    while ((SECONDS < deadline)); do
        for candidate in "${candidates[@]}"; do
            ((SECONDS < deadline)) || break
            rows=$(sql "$candidate" 'SHOW FRONTENDS') || continue
            master=$(master_from <<< "$rows")
            [[ -n $master ]] || continue
            validate_ip "$master"
            saw_master=true
            # Recheck the master's own view; it may have changed during discovery.
            verified=$(sql "$master" 'SHOW FRONTENDS') || continue
            [[ $(master_from <<< "$verified") == "$master" ]] || continue
            master_rows=$verified
            return 0
        done
        sleep "$POLL_INTERVAL"
    done
    master=""
    return 1
}

check_clock() {
    # BDB refuses a replica whose clock is more than max_bdbje_clock_delta_ms
    # (5 s) from the master's, but only after ROLE and VERSION are written.
    # Refuse before any metadata exists, with the cause in the allocation log.
    local remote local_now skew
    remote=$(sql "$1" 'SELECT UNIX_TIMESTAMP()' | awk 'NR == 2') ||
        fail "Cannot read the clock of FE $1"
    [[ $remote =~ ^[0-9]+$ ]] || fail "Cannot read the clock of FE $1"
    local_now=$(date +%s)
    skew=$((remote - local_now))
    ((skew >= 0)) || skew=$((-skew))
    ((skew <= MAX_CLOCK_SKEW_SECONDS)) ||
        fail "Local clock differs from FE $1 by $skew s (limit $MAX_CLOCK_SKEW_SECONDS s); synchronize NTP on every Doris host before starting"
}

peer_helper() {
    # BDB creates a new replication group only when a node's helper is the
    # node itself. Naming a peer instead lets a healthy member start from its
    # local group and makes a half-joined node fail and reschedule rather
    # than silently found a second cluster. A single FE has no peer.
    local candidate
    for candidate in "${candidates[@]}"; do
        [[ $candidate == "$NODE_IP" ]] || { echo "$candidate"; return; }
    done
    echo "$NODE_IP"
}

write_endpoint() {
    validate_ip "$1"
    printf 'export FE_MASTER_IP=%s\nexport FE_MASTER_PORT=9010\n' "$1" \
        > "$ALLOC_DATA/endpoint.env.tmp"
    mv "$ALLOC_DATA/endpoint.env.tmp" "$ALLOC_DATA/endpoint.env"
    echo "prestart: prepared $NODE_KIND at $NODE_IP using FE $1"
}

prepare_config() {
    mkdir -p "$ALLOC_DATA/conf"
    cp -a "$DORIS_HOME/$NODE_KIND/conf/." "$ALLOC_DATA/conf/"
    local conf="$ALLOC_DATA/conf/$NODE_KIND.conf"
    if [[ -n $CONFIG_OVERRIDES_FILE ]]; then
        # FE's Java Properties parser also accepts colon/whitespace separators
        # and escaped/continued keys. Restrict fragments to the shared, explicit
        # key=value syntax before checking keys tied to the pack's topology.
        if ! awk '
            /^[[:space:]]*($|#|!)/ { next }
            /^[[:space:]]*[A-Za-z_][A-Za-z_0-9]*[[:space:]]*=/ {
                if ($0 ~ /\\[[:space:]]*$/) exit 1
                next
            }
            { exit 1 }
        ' "$CONFIG_OVERRIDES_FILE"; then
            fail "Use single-line key = value configuration without escaped keys or continuations"
        fi
        # These settings are coupled to the pack's mounts, reserved ports and
        # upstream ASSIGN-mode scripts. Overriding them breaks the deployment.
        local managed='meta_dir|storage_root_path|priority_networks|initial_root_password|enable_fqdn_mode|frontend_address|deploy_mode|http_port|rpc_port|query_port|edit_log_port|be_port|webserver_port|heartbeat_service_port|brpc_port|arrow_flight_sql_port'
        if grep -Eq "^[[:space:]]*($managed)[[:space:]]*=" "$CONFIG_OVERRIDES_FILE"; then
            fail "Configuration overrides a pack-managed path, port or identity setting"
        fi
        printf '\n' >> "$conf"
        cat "$CONFIG_OVERRIDES_FILE" >> "$conf"
    fi
    # Preserve distribution defaults (including JVM flags). A host may carry one
    # IP alias per role, so pin each node to exactly its own address. Both
    # init_fe.sh and init_be.sh append a /24 to <kind>.conf, which could match
    # the other role's alias; <kind>_custom.conf is read afterwards and wins.
    printf 'priority_networks = %s/32\n' "$NODE_IP" > "$ALLOC_DATA/conf/${NODE_KIND}_custom.conf"
    chmod 600 "$ALLOC_DATA/conf/${NODE_KIND}_custom.conf"
    if [[ $NODE_KIND == fe ]]; then
        # MySQL PASSWORD() format of the exact Vault bytes:
        # '*' + uppercase hex(SHA1(SHA1(password))).
        local attempt
        for attempt in 1 2 3 4 5; do
            [[ -s $ROOT_PASSWORD_FILE ]] && break
            sleep 0.2
        done
        if [[ ! -s $ROOT_PASSWORD_FILE ]]; then
            echo "prestart: diagnostic: ROOT_PASSWORD_FILE=$ROOT_PASSWORD_FILE (after $attempt attempts)" >&2
            ls -la "$(dirname "$ROOT_PASSWORD_FILE")" >&2 2>&1 || echo "prestart: diagnostic: cannot list $(dirname "$ROOT_PASSWORD_FILE")" >&2
            wc -c "$ROOT_PASSWORD_FILE" >&2 2>&1 || echo "prestart: diagnostic: cannot stat $ROOT_PASSWORD_FILE" >&2
            echo "prestart: diagnostic: mount info for secrets dir:" >&2
            mount 2>&1 | grep -i secret >&2 || echo "prestart: diagnostic: no 'secret' mount entries found" >&2
            cat /proc/mounts 2>&1 | grep -i secret >&2 || echo "prestart: diagnostic: no 'secret' entries in /proc/mounts" >&2
            echo "prestart: diagnostic: stat of secrets dir itself:" >&2
            stat "$(dirname "$ROOT_PASSWORD_FILE")" >&2 2>&1
            fail "Vault root password is empty"
        fi
        local hash
        hash=$(openssl dgst -sha1 -binary "$ROOT_PASSWORD_FILE" |
            openssl dgst -sha1 -r | awk '{print "*" toupper($1)}')
        printf 'initial_root_password = %s\n' "$hash" >> "$conf"
        printf 'meta_dir = %s/fe/doris-meta\n' "$DORIS_HOME" >> "$conf"
    else
        # One storage root per mounted disk (storage/data1, storage/data2, ...).
        : "${BE_DISK_COUNT:?}"
        [[ $BE_DISK_COUNT =~ ^[1-9][0-9]*$ ]] || fail "BE_DISK_COUNT must be a positive integer"
        local paths="" i
        for ((i = 1; i <= BE_DISK_COUNT; i++)); do
            paths+="${paths:+;}$DORIS_HOME/be/storage/data$i"
        done
        printf 'storage_root_path = %s\n' "$paths" >> "$conf"
    fi
    chmod 600 "$conf"
}

main() {
    : "${NODE_KIND:?}" "${NODE_IP:?}" "${BOOTSTRAP_IP:?}" "${FE_CANDIDATES:?}"
    [[ $NODE_KIND == fe || $NODE_KIND == be ]] || fail "NODE_KIND must be fe or be"
    [[ $DISCOVERY_TIMEOUT =~ ^[1-9][0-9]*$ ]] || fail "Invalid discovery timeout"
    [[ $EXISTING_FE_DISCOVERY_TIMEOUT =~ ^[1-9][0-9]*$ ]] || fail "Invalid existing FE discovery timeout"
    [[ $MAX_CLOCK_SKEW_SECONDS =~ ^[0-9]+$ ]] || fail "Invalid clock skew limit"
    validate_ip "$NODE_IP"
    validate_ip "$BOOTSTRAP_IP"
    local -a candidates
    read -r -a candidates <<< "$FE_CANDIDATES"
    local candidate
    if [[ -n $CONSUL_FE_FILE ]]; then
        # Healthy FEs registered in Consul, rendered once when the task starts.
        # Try them before the static seeds; the seeds still cover bootstrap and
        # an unavailable catalog.
        local -a registered unique=()
        local -A seen=()
        read -r -d '' -a registered < "$CONSUL_FE_FILE" || true
        # Seeds are usually registered too; query each FE once per round.
        for candidate in "${registered[@]}" "${candidates[@]}"; do
            [[ -z ${seen[$candidate]:-} ]] || continue
            seen[$candidate]=1
            unique+=("$candidate")
        done
        candidates=("${unique[@]}")
    fi
    for candidate in "${candidates[@]}"; do validate_ip "$candidate"; done
    prepare_config

    local meta="$DORIS_HOME/fe/doris-meta" master master_rows saw_master
    if [[ $NODE_KIND == fe ]]; then
        if [[ -e $meta/image/ROLE || -e $meta/image/VERSION ]]; then
            # Doris writes ROLE, then VERSION, then image and BDB membership
            # (Env.getClusterIdAndRole). Any interruption in between leaves a
            # node that looks initialized but is not a BDB member; started
            # with itself as helper it founds a one-node group and exits
            # forever. With the elected master as helper, a healthy member
            # reads its local group as usual while a half-joined one resumes
            # the join exactly as a new node would. init_fe.sh passes
            # FE_MASTER_IP != FE_CURRENT_IP as --helper.
            if discover_master "$EXISTING_FE_DISCOVERY_TIMEOUT"; then
                if [[ $master == "$NODE_IP" ]]; then
                    write_endpoint "$NODE_IP"
                    return
                fi
                # The master's list is persistent; a member is always on it.
                lists_self <<< "$master_rows" ||
                    fail "FE $NODE_IP has metadata but is not listed by master $master (dropped, or its IP changed); review the cluster before intervening"
                check_clock "$master"
                write_endpoint "$master"
                return
            fi
            # Cold start or master outage: existing FEs must start
            # concurrently to form a quorum.
            write_endpoint "$(peer_helper)"
            return
        fi
        # An image or BDB directory without identity is not an empty new node.
        if [[ -d $meta/bdb || -d $meta/image ]]; then
            fail "Incomplete FE metadata; refusing to initialize over it"
        fi
    fi

    # Only discover the current master. Membership is left to the unchanged
    # image entrypoint: in ASSIGN mode init_fe.sh / init_be.sh check SHOW
    # FRONTENDS / SHOW BACKENDS and run ALTER SYSTEM ADD FOLLOWER / BACKEND
    # for a node with empty metadata or storage, then start it.
    if discover_master "$DISCOVERY_TIMEOUT"; then
        if [[ $NODE_KIND == fe ]]; then
            # init_fe.sh starts FE_MASTER_IP == FE_CURRENT_IP as a new master
            # without --helper. A node without metadata can never be the
            # elected master, so this would create a second cluster.
            [[ $master != "$NODE_IP" ]] ||
                fail "An FE without metadata is reported as master; check its volume"
            check_clock "$master"
        fi
        write_endpoint "$master"
        return
    fi

    if [[ $NODE_KIND == fe && $NODE_IP == "$BOOTSTRAP_IP" && $saw_master == false ]]; then
        # Name the exact host volume so the operator never has to search for it.
        : "${META_VOLUME:?}" "${NODE_NAME:?}"
        local where="host volume $META_VOLUME on Nomad node $NODE_NAME"
        if [[ -e $meta/.bootstrap-consumed ]]; then
            fail "No usable master before timeout, and the bootstrap permit in $where was already consumed; review the cluster state before intervening"
        fi
        if [[ -f $meta/.bootstrap-approved ]]; then
            # A permit is provisioned ONLY for a brand-new cluster. Consume before
            # launch: failure before ROLE/VERSION requires explicit operator review.
            mv "$meta/.bootstrap-approved" "$meta/.bootstrap-consumed"
            write_endpoint "$NODE_IP"
            return
        fi
        fail "No usable master before timeout. ONLY for a brand-new cluster: stop job ${NOMAD_JOB_NAME:-<job>}, create .bootstrap-approved in $where by running scripts/bootstrap-permit.sh from the bastion with the same arguments as nomad-pack run, then run nomad-pack run again"
    fi
    fail "No usable master before timeout; bootstrap requires an unused permit on the designated FE"
}

main "$@"
