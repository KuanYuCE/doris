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
ROOT_PASSWORD_HASH_FILE=${ROOT_PASSWORD_HASH_FILE:-/secrets/root-password-hash}
DISCOVERY_TIMEOUT=${DISCOVERY_TIMEOUT:-300}
POLL_INTERVAL=${POLL_INTERVAL:-2}

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

member_present() {
    local port_column=$1 port=$2
    awk -F '\t' -v ip="$NODE_IP" -v pc="$port_column" -v port="$port" '
        NR == 1 { for (i=1; i<=NF; i++) c[$i]=i; next }
        c["Host"] && c[pc] && $(c["Host"]) == ip && $(c[pc]) == port { found=1 }
        END { exit !found }'
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
    # Preserve distribution defaults (including JVM flags). Both upstream init
    # scripts append a /24 priority_networks on new nodes, so use the same subnet.
    printf '\npriority_networks = %s.0/24\n' "${NODE_IP%.*}" >> "$conf"
    if [[ $NODE_KIND == fe ]]; then
        local hash
        hash=$(< "$ROOT_PASSWORD_HASH_FILE")
        [[ $hash =~ ^\*[A-F0-9]{40}$ ]] || fail "Invalid initial root password hash"
        printf 'initial_root_password = %s\n' "$hash" >> "$conf"
        printf 'meta_dir = %s/fe/doris-meta\n' "$DORIS_HOME" >> "$conf"
    else
        printf 'storage_root_path = %s/be/storage\n' "$DORIS_HOME" >> "$conf"
    fi
    chmod 600 "$conf"
}

main() {
    : "${NODE_KIND:?}" "${NODE_IP:?}" "${BOOTSTRAP_IP:?}" "${FE_CANDIDATES:?}"
    [[ $NODE_KIND == fe || $NODE_KIND == be ]] || fail "NODE_KIND must be fe or be"
    [[ $DISCOVERY_TIMEOUT =~ ^[1-9][0-9]*$ ]] || fail "Invalid discovery timeout"
    validate_ip "$NODE_IP"
    validate_ip "$BOOTSTRAP_IP"
    local -a candidates
    read -r -a candidates <<< "$FE_CANDIDATES"
    local candidate
    for candidate in "${candidates[@]}"; do validate_ip "$candidate"; done
    prepare_config

    local meta="$DORIS_HOME/fe/doris-meta"
    if [[ $NODE_KIND == fe ]]; then
        if [[ -f $meta/image/ROLE && -f $meta/image/VERSION ]]; then
            # Do not wait for SQL/quorum: existing FEs must start concurrently.
            write_endpoint "$NODE_IP"
            return
        fi
        if [[ -e $meta/image/ROLE || -e $meta/image/VERSION ]]; then
            fail "Incomplete FE metadata; restore the volume before restarting"
        fi
        # An image or BDB directory without identity is not an empty new node.
        if [[ -d $meta/bdb || -d $meta/image ]]; then
            fail "Incomplete FE metadata; refusing to initialize over it"
        fi
    fi

    local deadline=$((SECONDS + DISCOVERY_TIMEOUT)) master rows verified
    local saw_master=false
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
            if [[ $NODE_KIND == fe ]]; then
                if ! member_present EditLogPort 9010 <<< "$verified"; then
                    sql "$master" "ALTER SYSTEM ADD FOLLOWER '$NODE_IP:9010'" >/dev/null || continue
                    verified=$(sql "$master" 'SHOW FRONTENDS') || continue
                    member_present EditLogPort 9010 <<< "$verified" || continue
                fi
            else
                rows=$(sql "$master" 'SHOW BACKENDS') || continue
                if ! member_present HeartbeatPort 9050 <<< "$rows"; then
                    sql "$master" "ALTER SYSTEM ADD BACKEND '$NODE_IP:9050'" >/dev/null || continue
                    rows=$(sql "$master" 'SHOW BACKENDS') || continue
                    member_present HeartbeatPort 9050 <<< "$rows" || continue
                fi
            fi
            write_endpoint "$master"
            return
        done
        sleep "$POLL_INTERVAL"
    done

    if [[ $NODE_KIND == fe && $NODE_IP == "$BOOTSTRAP_IP" && $saw_master == false &&
          -f $meta/.bootstrap-approved && ! -e $meta/.bootstrap-consumed ]]; then
        # A permit is provisioned ONLY for a brand-new cluster. Consume before
        # launch: failure before ROLE/VERSION requires explicit operator review.
        mv "$meta/.bootstrap-approved" "$meta/.bootstrap-consumed"
        write_endpoint "$NODE_IP"
        return
    fi
    fail "No usable master/registration before timeout; bootstrap requires an unused permit on the designated FE"
}

main "$@"
