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

# Consul script check run inside the main Doris container:
#   ready.sh fe <node-ip>
#   ready.sh be <node-ip> <fe-ip>...
# Exit 0 is passing; exit 2 is critical (Consul treats 1 as warning).

set -uo pipefail

DORIS_MY_CNF=${DORIS_MY_CNF:-/secrets/my.cnf}

critical() { echo "$*"; exit 2; }

sql() {
    # Same bounded, credential-free argv as prestart.
    timeout 5 mysql --defaults-file="$DORIS_MY_CNF" --connect-timeout=2 \
        --batch --raw -uroot -P9030 -h "$1" -e "$2" 2>/dev/null
}

# Succeeds when a row has Host=$1, $2=$3 and every remaining column=value pair.
row_matches() {
    awk -F '\t' -v args="$*" '
        BEGIN { n = split(args, a, " ") }
        NR == 1 { for (i=1; i<=NF; i++) c[$i]=i; next }
        {
            if (!c["Host"] || $(c["Host"]) != a[1]) next
            for (i=2; i<n; i+=2) if (!c[a[i]] || $(c[a[i]]) != a[i+1]) next
            found=1
        }
        END { exit !found }'
}

main() {
    local kind=$1 ip=$2 rows
    shift 2
    if [[ $kind == fe ]]; then
        # Ask the local FE: its own row must be joined and alive, and it must
        # see an alive elected master, i.e. it can serve metadata.
        rows=$(sql "$ip" 'SHOW FRONTENDS') || critical "FE $ip does not answer authenticated SQL"
        row_matches "$ip" EditLogPort 9010 Join true Alive true <<< "$rows" ||
            critical "FE $ip is not joined and alive"
        awk -F '\t' '
            NR == 1 { for (i=1; i<=NF; i++) c[$i]=i; next }
            c["IsMaster"] && c["Alive"] && $(c["IsMaster"]) == "true" && $(c["Alive"]) == "true" { found=1 }
            END { exit !found }' <<< "$rows" || critical "FE $ip sees no alive master"
        echo "FE $ip is ready"
        return
    fi
    [[ $kind == be ]] || critical "Unknown node kind: $kind"
    # BASH_ENV (endpoint.env) supplies the master chosen by prestart; the
    # seeds cover a later master change. Any FE can answer SHOW BACKENDS.
    local fe
    for fe in ${FE_MASTER_IP:+"$FE_MASTER_IP"} "$@"; do
        rows=$(sql "$fe" 'SHOW BACKENDS') || continue
        row_matches "$ip" HeartbeatPort 9050 Alive true <<< "$rows" ||
            critical "BE $ip is not registered and alive according to FE $fe"
        echo "BE $ip is ready according to FE $fe"
        return
    done
    critical "No FE answers authenticated SQL"
}

main "$@"
