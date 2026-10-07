# Doris on Nomad（Docker driver）

這是一個保留官方 image 主程序 entrypoint 的 Nomad Pack 範例。首次部署與擴容
使用同一套 pack；FE/BE groups 同時排程，由各自的 `prestart` 等待必要條件。
不修改 `docker/runtime` 原有腳本，也不另建 Doris image。

## 檔案與啟動流程

- `templates/doris.nomad.tpl`：每個固定節點一個 group，包含 `prepare` 和 `doris` tasks。
  資料卷在 group 宣告一次；FE 的 `prepare` 需讀寫 metadata（判斷 `ROLE`／`VERSION`、
  消耗 bootstrap permit）才掛載，BE 的 `prepare` 不掛載 storage。
- `templates/_credentials.tpl`：透過 Vault KV v2 與 Nomad HCL `template` blocks 產生權限 `0600` 的 `.my.cnf`。
- `templates/_fe-config.tpl`、`templates/_be-config.tpl`：獨立的 FE／BE 設定片段 templates。
- `scripts/prestart.sh`：在對應的 Doris image 內準備設定、探索 master、把關 bootstrap。
- `scripts/consul_ready.sh`：Consul script check，在主容器內以已認證 SQL 判斷節點是否可用。
- `scripts/wipe-volumes.sh`：在 bastion 執行，**清空 job 所有 host volume 的資料**，用於重建叢集（見「重建叢集」）。
- `scripts/ops-common.sh`：`wipe-volumes.sh` 使用的 Nomad 查詢、render 與 SSH 函式；`bootstrap-permit.sh` 內含相同函式的副本以便獨立執行，測試會檢查兩者一致。
- `scripts/bootstrap-permit.sh`：在 bastion 執行、可獨立執行的操作人員工具。必須在 `nomad-pack run` 前執行：從 var-file 找出 bootstrap FE，經 Nomad node API 查出 meta volume 路徑，並經 SSH 建立一次性授權檔。
- `examples/cluster.hcl`：pack 的 **var-file** 範例（3 FE + 3 BE），以 `nomad-pack -f` 傳入。
- `examples/client.hcl`：Nomad client agent 設定（持久化 host volumes），**不是** var-file。

```text
prepare task (prestart, sidecar=false)
  ├─ 複製該 image 的 conf 到 /alloc/data/conf
  ├─ FE 有 ROLE + VERSION → 直接放行，不等 SQL/master
  ├─ 新 FE / BE → 輪詢 Consul 中健康的 FE + discovery seeds
  │              → SHOW FRONTENDS 找現任 master（只讀）
  └─ 寫入 /alloc/data/endpoint.env
                ↓
doris task
  ├─ BASH_ENV=/alloc/data/endpoint.env
  ├─ /root/.my.cnf ← 該 task 的 secrets/my.cnf
  ├─ /opt/apache-doris/{fe,be}/conf ← prestart 複製的設定
  └─ 原本 image 的 ENTRYPOINT（不覆寫 command / args / entrypoint）
       └─ ASSIGN 模式：新節點由 init_fe.sh / init_be.sh 自行
          ALTER SYSTEM ADD FOLLOWER / BACKEND 後啟動
                ↓
Consul services
  ├─ <job>-fe：9030，TCP + sql-ready script check
  ├─ <job>-fe-http：8030，GET /api/health
  ├─ <job>-fe-editlog：9010，TCP
  ├─ <job>-be：9050，TCP + sql-ready script check
  └─ <job>-fe-flight／<job>-be-flight：8070／8050（Arrow Flight SQL），TCP
```

### prestart 與原入口的分工

原入口已經會註冊新成員，prestart 不再下任何 `ALTER SYSTEM`，只做原入口做不到的事：

| 工作 | 負責者 | 原因 |
| --- | --- | --- |
| `SHOW FRONTENDS`/`SHOW BACKENDS` 檢查、`ALTER SYSTEM ADD FOLLOWER/BACKEND` | 原入口 | `init_fe.sh` 在 metadata 為空時執行；`init_be.sh` 只看 `storage/data`，多磁碟配置下資料在 `storage/data<N>/`，所以每次啟動都會先查 `SHOW BACKENDS`，已註冊就略過 |
| 找出現任 master | prestart | ASSIGN 模式需要固定的 `FE_MASTER_IP`，原入口不會探索；master 會漂移 |
| 決定是否 bootstrap 新叢集 | prestart | 原入口看到 `FE_MASTER_IP == FE_CURRENT_IP` 就以新 master 啟動，沒有防止第二個叢集的機制 |
| 既有 FE 不等 quorum 直接放行 | prestart | 將 `FE_MASTER_IP` 設為自己，原入口直接啟動既有 metadata |
| 複製設定、附加片段、`initial_root_password` | prestart | 原入口不支援設定片段或密碼 hash |

代價：原入口的 `check_fe_registered` 在節點尚未註冊時會先輪詢 60 秒才執行
`ALTER SYSTEM ADD FOLLOWER`，所以新 FE 加入約多等一分鐘。原入口把 SQL 錯誤導到
`/dev/null`；註冊失敗時看 `doris` task 的 `Failed to register ...` 訊息，並用
prestart 日誌確認當時使用的 master 與認證都正常。

`BASH_ENV` 利用 Bash 啟動時載入環境檔的功能，只設定 `FE_MASTER_IP` 與
`FE_MASTER_PORT`。它不取代原入口，也不常駐執行 discovery。只有短命的
`prepare` task 覆寫自己的入口，以執行 prestart 腳本。

## 一份 jobspec，不共用設定檔

`doris.nomad.tpl` 是產生 Nomad job 的共同模板，不是 Doris 設定檔。每個 group
有自己的 allocation；即使容器內都叫 `/alloc/data/conf`，它們也指向不同實體目錄。
同一個 allocation 裡，只有 `prepare` 與 `doris` tasks 共用這個目錄。

FE 從自己的 image 複製 `fe/conf`，加上 `_fe-config.tpl` 產生的設定，再掛載到
`/opt/apache-doris/fe/conf`；BE 對應的是 `be/conf`、`_be-config.tpl` 與
`/opt/apache-doris/be/conf`。可以在 pack 變數檔分別調整：

```hcl
fe_config = <<EOF
sys_log_level = INFO
EOF

be_config = <<EOF
sys_log_level = WARNING
EOF
```

兩份設定是附加於 image defaults 的片段，避免重寫完整配置時遺漏 JVM flags 等
發行版預設值。改 `fe_config` 不會改動 BE groups，反之亦然。但改 FE 共用設定
仍會影響所有 FE groups，套用前需安排逐組 rollout 或維護停機。
與 pack 綁定的資料路徑、ports、`priority_networks`、`initial_root_password`、
FQDN／部署模式等不能在片段覆寫，prestart 會報錯。
片段使用單行 `key = value`；不支援 `key: value`、省略等號、跳脫 key 或反斜線續行。

## 前提

本範例以 master `288e89103d` 的 Docker scripts 為依據；指定 image 必須具有相同契約：

- FE entrypoint 為 `bash init_fe.sh`，BE 為 `bash entry_point.sh`。
- 程式位於 `/opt/apache-doris`，支援 ASSIGN 模式與 FE `initial_root_password`。
- image 內有 Bash、mysql client、OpenSSL、GNU coreutils（含 `timeout`）及 awk。
- 使用 IPv4、host network、標準 Doris ports；每台 client 至多一個 FE、一個 BE。
- hostname 是 **Nomad client node name**，不是 Doris FQDN；Doris membership 使用 `ip`。
- IP 必須確實屬於該 host。同一台 host 可用 IP alias 讓 FE 與 BE 各用一個位址：
  prestart 把 `priority_networks = <ip>/32` 寫進 `fe_custom.conf`／`be_custom.conf`。
  原有 entrypoint 仍會在 `fe.conf`／`be.conf` 附加 `/24`，但 Doris 在讀完
  `<kind>.conf` 後才讀 `<kind>_custom.conf` 並以其覆蓋，所以生效的是 `/32`。
- Nomad client 已配置持久化 host volumes；資料不能放在 ephemeral allocation 目錄。
- 需要 Consul：每台 client 需有 Consul agent，Nomad 已完成 Consul 整合（見「Consul 前提」）。
- Nomad client 以 systemd 執行時，不可啟用 `PrivateTmp`、`PrivateDevices`、
  `ProtectHome`、`ProtectSystem` 等會建立 mount namespace 的選項，請沿用官方 unit。
  Nomad 在 task 的 `secrets/` 掛載的 tmpfs 只存在 nomad 的 namespace，dockerd 看不到，
  container 內的 `/secrets/*` 會是空的；`MountFlags=shared` 也無法恢復傳播。
- 範例 FE memory 為 16 GiB，以容納 image 常見的 8 GiB JVM heap；請對照實際 image
  與主機容量配置資源。FE 與 BE 同機時，兩者 reservation 必須都能滿足。
- 依 Doris 正式部署要求預先設定主機，例如 BE 的 `vm.max_map_count`、磁碟、時鐘同步等。

支援 **Nomad Pack 0.4.1 以上**。模板從 root variable file 的絕對路徑推得 pack 目錄，
再以 `fileContents` 讀取 `scripts/`；不使用 0.4.2 才提供的 `meta "pack.path"`。
本地驗證工具版本：Nomad Pack 0.4.1／0.4.2、Nomad 2.0.4／2.0.7。範例 image tag 為使用者指定的
`apache/doris:fe-4.1.4`／`apache/doris:be-4.1.4`；尚未對這兩個發行 image 執行叢集整合測試。
上線前先檢查 image 的 entrypoint、工具、設定及實際 SQL 輸出；正式使用建議固定 digest。

## 一次性準備

1. 在每台指定 client 準備 host volumes，名稱固定為 `doris-<fe|be>-b<block_index>-d<disk>`
   （`templates/_helpers.tpl` 的 `blockVolumeName`）：FE 使用 `d0`（meta）與 `d1`（log），
   BE 使用 `d0` 到 `d<be_disks_per_block - 1>`。可由 dynamic host volume plugin 建立，
   或參考 `examples/client.hcl` 以 static host volume 設定；同一檔案也列出 job
   constraints 需要的 `node_pool` 與 client `meta`。這些路徑必須位於正確的持久磁碟，
   不能在磁碟缺失時退回空目錄。
2. 複製 `examples/cluster.hcl` 到你自己的設定檔，填入 client name、IP、`block_index`，
   以及 `fe_image_repo`／`be_image_repo`／`doris_version`。
   `bootstrap_fe` 是首次建立叢集的唯一 FE，不代表永久 master。
   `discovery_fe_ips` 是穩定的 discovery seeds，建議為最初三台 FE。
3. 使用既有的 Vault KV v2 secret：mount 為 `kv-data`，secret 路徑為
   `doris-secret/bootstrap`，欄位為 `password`。pack 變數如下：

   ```hcl
   vault_role         = "doris"
   vault_secret_path  = "kv-data/data/doris-secret/bootstrap"
   vault_password_key = "password"
   ```

   `vault_secret_path` 是 **API 路徑**，所以 KV v2 的 mount 後需要 `/data/`。
   `vault_password_key` 只能使用英數字與底線（`[A-Za-z_][A-Za-z0-9_]*`），render 時檢查。
   secret 中缺少這個欄位時，template 會直接失敗，不會產生空密碼檔。
   Nomad server/client 須已完成 Vault workload identity 整合（包含 default identity），
   `vault_role` 請填入你實際使用的 Vault JWT auth role。角色所附 policy 至少需要：

   ```hcl
   path "kv-data/data/doris-secret/bootstrap" {
     capabilities = ["read"]
   }
   ```

   Nomad 在 task 啟動時透過 `secret` template function 讀取 Vault，並把密碼寫成
   MySQL option file；`nomad-pack render` 階段不存取 Vault，也不會把密碼嵌入 jobspec。
   `.my.cnf` 最終仍是 INI 格式，HCL 是描述它如何產生的設定。
   Template 會跳脫密碼中的反斜線、雙引號、換行、CR 與 tab。

   FE 的 `prepare` task 額外把同一 secret 的密碼原始 bytes 寫到自己的 `secrets/`
   目錄，用 OpenSSL 計算 `* + uppercase(hex(SHA1(SHA1(password))))`，再寫入
   `fe.conf` 的 `initial_root_password`。Vault 不需要另外儲存 hash；原因與兩種
   檔案格式見「一份密碼、兩種格式」。
   第一次建立叢集就使用這組密碼，不需等 FE 起來後再執行 `SET PASSWORD`。

   Vault token 不注入容器的環境變數或檔案；Nomad 管理 token 與 template 渲染。
   這是固定 KV root 密碼的流程，不是 Vault database engine 的動態帳號流程。

4. **僅限確定為全新叢集**：在 `nomad-pack run` **之前**，於 bastion 上建立
   bootstrap FE 的一次性授權檔（放在 `doris-fe-b<block_index>-d0` 資料卷）。
   `scripts/bootstrap-permit.sh` **接受與 `nomad-pack run` 相同的參數**（最後一個是
   pack 名稱或路徑），以本機 `nomad-pack render` 讀出 bootstrap FE，不需要 job 已提交；
   再從 Nomad node API 查出該 host volume 的實際路徑，經 SSH 建立。請在與
   `nomad-pack run` 相同的目錄、以相同的參數與順序執行（nomad-pack 疊加多個 var-file
   時的結果與路徑寫法有關，這樣才能保證兩者一致）：

   ```bash
   export NOMAD_TOKEN=...   # token 只接受環境變數，避免出現在 ps 與 shell history
   ARGS=(--registry=doris_packs --ref=pack-template \
         --address=https://nomad.example:4646 --ca-cert=/etc/nomad.d/ca.pem \
         --namespace=doris --var-file=/path/to/cluster.hcl doris)

   scripts/bootstrap-permit.sh --dry-run "${ARGS[@]}"   # 確認節點、volume、路徑與 SSH 目標
   scripts/bootstrap-permit.sh "${ARGS[@]}"             # 經 SSH 建立授權檔
   nomad-pack run "${ARGS[@]}"                          # 部署，之後不需要再下任何指令
   ```

   參數分流：`--registry`、`--ref`、`--var-file`（`-f=`）、`--var` 依原順序交給
   `nomad-pack render`；`--address`、`--ca-cert`、`--client-cert`、`--client-key`、
   `--tls-server-name`、`--region`、`--namespace` 交給 nomad CLI（`-flag=` 與
   `--flag=` 皆可，一律寫成 `=` 形式），也可改用對應的 `NOMAD_*` 環境變數。
   `--token` 會被拒絕。沒有給 pack 時，render 腳本所在的 pack（`scripts/..`）。

   **必須在 `nomad-pack run` 之前執行完成。** 腳本會在 job 可能所在的每個 namespace
   （render 出的 `namespace`、`--namespace`、`NOMAD_NAMESPACE`）檢查 job：任一處已註冊
   且仍在執行（未 stop，或仍有 running／pending allocations）就拒絕，請先 `nomad job stop`；
   都不存在或已 stop（例如剛用 `wipe-volumes.sh` 清空）才會執行。

   腳本可以**獨立執行**，不依賴 pack 內其他 script，只需要 `nomad`、`nomad-pack`、`ssh`；
   從 registry 部署時，bastion 上已有的 `nomad-pack registry` 設定就足夠。

   建立授權檔時預設執行 `ssh <節點在 Nomad 回報的 IP>`，可用 `--ssh-host=[user@]host`
   覆寫；金鑰、ProxyJump、port 等設定請寫在 bastion 的 `~/.ssh/config`。遠端以
   `sudo -n` 執行，該帳號需要免密碼 sudo。遠端主機必須擁有該節點的 IP，且資料卷
   存在、沒有 `image`、`bdb`、`.bootstrap-consumed`，才會建立檔案；`--ssh-host`
   指到錯的機器時會被拒絕。若忘了先建立授權檔就部署，bootstrap FE 會在
   `discovery_timeout` 後失敗，prepare task 的 log 會印出應放置的 volume 與節點，
   並提示先 stop job、執行本腳本、再重新 `nomad-pack run`。

   其他 FE 不建立此檔。prestart 先嘗試發現既有 master；逾時且沒有看到 master，
   才消耗這份授權並允許初始化。這個操作是磁碟初次佈署的一部分，不需另一套 pack。

## 部署

`bootstrap_fe`、`discovery_fe_ips`、`fe_nodes`、`be_nodes` 沒有預設值，必須由 var-file
提供；其他變數的預設值見 `variables.hcl`。只想確認 pack 能否 render 與通過驗證時，
可直接使用範例 var-file（不需連線 Nomad、Consul 或 Vault）：

```bash
nomad-pack render docker/runtime/nomad -f docker/runtime/nomad/examples/cluster.hcl \
  --to-dir /tmp/doris-render --auto-approve
nomad job validate /tmp/doris-render/doris/doris.nomad
```

實際部署時，使用依自己環境修改後的 var-file：

```bash
nomad-pack render docker/runtime/nomad -f /path/to/cluster.hcl \
  --to-dir /tmp/doris-render --auto-approve
nomad job validate /tmp/doris-render/doris/doris.nomad
nomad-pack plan docker/runtime/nomad -f /path/to/cluster.hcl --name doris
nomad-pack run docker/runtime/nomad -f /path/to/cluster.hcl --name doris
```

只需執行一個 pack，之後不需要再下指令。第一次部署時 bootstrap FE 會先探測
`discovery_timeout`（預設 120 秒）確認沒有既有 master，才消耗授權檔、以 master 啟動。
這段期間其他 FE／BE 可能逾時失敗，Nomad UI 會暫時看到 failed allocations；它們會
依 `reschedule` 自動重新排程（指數退避，最長 2 分鐘），等 master 起來後自行加入。
這是預期行為，不需介入。Doris 初始化需要時間，請查看 `prepare` 日誌，而非只看
容器是否立即出現。

```bash
nomad job status doris
nomad alloc logs <allocation-id> prepare
nomad alloc logs <allocation-id> doris
```

`sql-ready` check 以已認證 SQL 確認 FE 已 Join、Alive 且看得到存活的 master，
BE 在 `SHOW BACKENDS` 中為 Alive。它**不代表 metadata 已追平或 BE 所有 tablet 都健康**，
更新下一台 FE 前仍應以 SQL 確認 `ReplayedJournalId` 等追趕狀態。

## Consul services

所有服務都註冊到 Consul（不支援 Nomad 內建 service provider：它沒有 script check，
也無法提供 prestart 的 FE 探索與 DNS）。所有 checks 都會作為 `update` 區塊的部署
健康依據：`max_parallel = 1` 的 group 更新會等節點真正可用，才算健康。

| 服務 | Port | Checks | Tags |
| --- | --- | --- | --- |
| `<job>-fe` | 9030（query） | TCP、`sql-ready` | `fe`, `query` |
| `<job>-fe-http` | 8030 | `GET /api/health` | `fe`, `http` |
| `<job>-fe-editlog` | 9010 | TCP | `editlog`, `fe-id-<index>` |
| `<job>-fe-flight` | 8070 | TCP | `flight`, `arrow-flight-sql`, `fe-id-<index>` |
| `<job>-be` | 9050（heartbeat） | TCP、`sql-ready` | `be`, `heartbeat` |
| `<job>-be-flight` | 8050 | TCP | `flight`, `arrow-flight-sql`, `be-id-<index>` |

`<index>` 是節點在 `fe_nodes`／`be_nodes` 中的位置。每個服務都帶
`meta.node = <Nomad client name>`。健康的 `<job>-fe` 也提供 prestart 的 FE 探索與
Consul DNS（例如 `doris-fe.service.consul`）。

### sql-ready check（`scripts/consul_ready.sh`）

**目的**：判斷節點是否真的可以使用，而不只是 port 已開啟。TCP check 在 9030／9050
開始監聽時就會通過，但此時 FE 可能尚未 Join、仍在追 metadata 或看不到 master，
BE 可能尚未註冊或 FE 尚未收到它的 heartbeat。`update` 區塊依 checks 判斷部署健康，
只看 TCP 時，逐台升級可能在上一台 FE 真的可用之前就開始更新下一台。

**判斷方式**：Nomad 在主容器內執行 `/bin/bash /local/consul_ready.sh`，使用同一份
`/secrets/my.cnf` 以 root 帳號執行 SQL：

- FE（`consul_ready.sh fe <ip>`）：詢問本機 FE 的 `SHOW FRONTENDS`，自身列
  （IP + EditLogPort 9010）須 `Join = true`、`Alive = true`，且看得到
  `IsMaster = true`、`Alive = true` 的列。
- BE（`consul_ready.sh be <ip> <seed>...`）：依序詢問 prestart 選定的 master
  （經 `BASH_ENV` 取得 `FE_MASTER_IP`）與 `discovery_fe_ips`，由第一台有回應的 FE
  確認自身列（IP + HeartbeatPort 9050）`Alive = true`。

通過時回傳 0（passing）；失敗時輸出原因（例如 `FE 10.0.0.12 is not joined and
alive`，可在 Consul UI 的 check output 查看）並回傳 2（critical），不回傳 1
（Consul 視為 warning）。

**影響範圍**：

- 部署：check 通過前，該 allocation 不算健康，不會進行下一步 rollout。
- Consul DNS 與服務查詢：只回傳通過 check 的節點，例如 `doris-fe.service.consul`。
- prestart 的 Consul 探索：`local/consul-fe` 只列出健康的 FE，新節點只向可用的 FE
  詢問 master。
- 不重啟 Doris：未設定 `check_restart`，master 暫時不可用等狀況只改變健康狀態。

**限制**：它不檢查 metadata 是否追平（例如 `ReplayedJournalId`），也不檢查
BE tablet 健康；升級下一台 FE 前仍應以 SQL 確認追趕狀態。

### `/api/health`

FE 尚未完成啟動時回 503；目前 master 程式碼中此端點一律不需認證。較舊版本在
`enable_all_http_auth = true` 時會要求認證，這個 check 會持續
失敗並卡住部署；請先在實際 image 上確認 `curl -i http://<fe>:8030/api/health`
回 200，否則升級 image 或關閉該設定。

### 從 Consul 探索 FE

`prepare` task 以 template 讀取 `<job>-fe` 中**健康**（`sql-ready` 通過）的位址，
寫到 `/local/consul-fe`，放在 `discovery_fe_ips` 前面依序嘗試並去重。
Template 使用 `once = true`，只在 task 啟動時讀一次。首次 bootstrap 時目錄為空，
仍依靠靜態 seeds 與 bootstrap permit；Consul 不可用時 template 會阻塞，重試用盡後
allocation 失敗並重新排程。因此 `discovery_fe_ips` 仍為必填。

### Consul 前提

- 每台 Nomad client 的 Consul agent 正常運作，Nomad client 已設定 `consul` 區塊。
- 啟用 Consul ACL 時，Nomad 須設定 Consul workload identity（Nomad 1.7+ 的
  `service_identity` 與 `task_identity`），讓服務註冊與 `prepare` task 的
  `service` template 取得 token；task identity 對應的 policy 至少需要
  `service "<job>-fe" { policy = "read" }` 與 `node_prefix "" { policy = "read" }`。

只有「已在運行、用舊版 pack 部署的叢集」升級到本版時，才需要注意：每個 group 的 services 與 env 都會改變，需依上文的逐組 rollout 或維護停機方式套用。全新部署不受影響。

## 重啟與 master 切換

既有 FE 的 `ROLE`、`VERSION` 都存在時，prestart 不等待 master，直接放行，讓 FEs
同時啟動並自行選舉。缺失部分 identity files 或已有不完整 metadata 時會退出，
不把它當作新叢集。

BE 每次新 allocation 都重新探索 master，新 BE 由原有 `init_be.sh` 註冊。
原有 BE entrypoint 的 status check 只等 60 秒：FE 若在此時故障或 master 切換，
主 task 可能退出。這版靠重新排程，再跑一次 discovery 收斂，並不承諾啟動過程完全沒有重試。
同理，新 FE 若在 prestart 後遇到 master 切換，原入口的 `ALTER SYSTEM` 仍可經
follower 轉送；若舊 master 已不可連線則該 allocation 失敗、重新排程後以新 master 重試。

已成功的 ephemeral prestart 不會因主 task 一般的 restart 自動再跑。因此設定為：

```hcl
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
```

主 task 失敗後產生新 allocation，沿用固定 client 的資料卷並重新跑 prestart。
手動重啟也使用新 allocation，例如：

```bash
nomad job restart -reschedule -group=be-doris-1 doris
```

不要只 restart `doris` task 並期待 prestart 重跑。日常 FE 維護逐台進行；全叢集
停機後恢復時則必須讓足夠既有 FE 同時起來形成 quorum，不能先等第一台恢復 quorum
才啟動下一台。此範例不做 metadata failure recovery，也不自動強制選主。

## 擴容與升級

新增 FE／BE 時，只在同一份 `cluster.hcl` 對應 list 的**最後**增加一個 map（overrides
以 index 為 key，插在中間會讓後面的節點對應錯誤），預先準備該節點的 host volumes，再執行同樣的 `plan`／`run`。**不要同時修改 `discovery_fe_ips`**：
把完整節點清單注入每個既有 group 會造成配置改變，使擴容變成全叢集更新。
Seeds 只需有一台可連線且能回報現任 master；master 本身不必在 seed 清單。
所有 seeds 都不可用時，新節點加入會等待，既有 FE 仍可啟動。

FE 建議維持適當的奇數個 electable members；本範例新增 FE 一律為 FOLLOWER。
不能藉由刪除 list entry 安全縮容：BE 要先 decommission，FE 要依 Doris membership
維護程序移除。本 pack 不自動 DROP 成員或刪除資料。

image 為 `<fe|be>_image_repo:<fe|be>-<doris_version>`。升級時不要直接改
`doris_version`（會一次改動所有 groups），而是用 `fe_version_overrides`／
`be_version_overrides`（以 list index 字串為 key，例如 `{ "0" = "4.1.5" }`）一次只改
一個 FE，plan 應只更換那個 FE group；確認 SQL 健康後再更新下一台，全部完成後再改
`doris_version` 並清空 overrides。`max_parallel=1` 是 **每個 group** 的限制，
並不保證不同 FE groups 依序更新。修改共用模板、資源參數或 seed 清單也可能影響
所有 groups；不要直接把這類更新套到正式叢集，需安排逐組 rollout 或維護停機。

## 重建叢集

要放棄現有資料、重新 bootstrap 時，host volume plugin 看到目錄已有資料會略過，
無法替你清空；請在 bastion 用 `scripts/wipe-volumes.sh`。它會**刪除該 job 所有
FE（meta、log）與 BE（每顆 disk）資料**，無法復原：

```bash
nomad job stop -namespace=doris doris        # 必須先停止，且沒有 running/pending allocations
scripts/wipe-volumes.sh --dry-run "${ARGS[@]}"        # 列出會清空的節點與路徑（ARGS 同上）
scripts/wipe-volumes.sh --confirm=doris "${ARGS[@]}"
scripts/bootstrap-permit.sh "${ARGS[@]}"
nomad-pack run "${ARGS[@]}"
```

- 參數與 `bootstrap-permit.sh` 相同（即 `nomad-pack run` 的參數）；group 與 volume
  來自本機 render，路徑來自 Nomad node API。連線與 SSH 設定也相同。
- 在 job 可能所在的每個 namespace（render 出的、`--namespace`、`NOMAD_NAMESPACE`）
  確認 job 不存在，或已 stop 且沒有 running／pending allocations，否則拒絕執行。
- `--confirm` 必須等於 var-file 的 `job_name`。所有節點與 volume 都查得到才開始刪除。
- 遠端主機必須擁有該節點的 IP；每個路徑都必須存在、是目錄、且不是 `/` 或第一層目錄
  （例如 `/data`），全部通過才刪除。只刪除目錄內容：保留目錄本身與 `lost+found`，
  不跨入其下掛載的其他檔案系統。

## 密碼、資料卷與 bootstrap 的界線

- `.my.cnf` 只提供 mysql client 密碼；原有腳本仍寫死 `-uroot`，不能藉此換成其他帳號。
- `initial_root_password` 不會改寫既有 metadata 的密碼。匯入已有叢集時，credentials
  必須匹配既有 root 密碼；空密碼叢集需先另外完成密碼遷移。
- secret templates 使用 `once=true`；本範例沒有自動密碼輪替。變更 Vault KV
  不等於執行 Doris `SET PASSWORD`，也不會讓既有 allocations 同步更新。不要在
  bootstrap 或 rollout 中途修改 secret，否則不同 tasks 可能取得不同版本。
  Vault token 更新使用 `change_mode="noop"`，不因 token 更新重啟 Doris。
- Bootstrap permit 在主 FE 啟動前就被改名為 `.bootstrap-consumed`。如果此時失敗、
  尚未產生 `ROLE`/`VERSION`，會停止自動 bootstrap；需由操作人員確認叢集狀態後處理。
  不要把授權檔的建立放進每次開機或每次 allocation 的初始化。
- 不可把 template 目錄或 `/alloc/data` 當作 Doris 資料卷；它們只放可重建設定。
- 固定 client 保護節點身分與本地磁碟對應，但 client 永久故障不會自動遷移資料。
- 此範例不配置 SQL TLS、網路防火牆或外部負載平衡器；依既有內網部署規範設定。

### 一份密碼、兩種格式：`my.cnf` 與 `root-password`

Vault KV v2 是唯一的密碼來源，secret 必須保存**明文** root 密碼。各 task 啟動時
從同一個 secret 產生兩種格式的檔案（皆 `once = true`、權限 `0600`），內容一定一致：

| 檔案 | 格式 | 產生於 | 用途 |
| --- | --- | --- | --- |
| `secrets/my.cnf` | INI，密碼經跳脫（`\`、`"`、換行、CR、tab） | 所有 FE／BE 的 `prepare` 與 `doris` tasks | mysql client 以 root 登入：原入口、prestart、`consul_ready.sh` |
| `secrets/root-password` | 原始 bytes，不跳脫、無前後空白 | 只有 FE 的 `prepare` task | prestart 計算 `initial_root_password` |

**為什麼要計算 hash**：Doris 的 `initial_root_password` 只接受 2-staged SHA-1 格式
`* + uppercase(hex(SHA1(SHA1(password))))`，例如 `root@123` 對應
`*A00C34073A26B40AB4307650BFB9309D6BFA6999`（等同 `SELECT PASSWORD('root@123')`）。
若誤填明文，FE 只記錄 WARN `initial_root_password is not valid 2-staged SHA-1
encrypted, ignore it` 並照常啟動，**root 會沒有密碼**。此設定也只在 master FE
第一次啟動時套用，之後修改不會生效。

**為什麼另存原始 bytes，而不從 `my.cnf` 計算**：Nomad template 沒有 SHA1 函式，
hash 只能在 prestart 計算；若從 `my.cnf` 計算，必須先在 Bash 中還原 INI 跳脫，
差一個 byte 就會產生錯誤的 hash，而且要到登入失敗才會發現。另存一份原始 bytes，
讓 OpenSSL 直接對正確輸入計算。

**為什麼只有 FE `prepare` 需要 `root-password`**：`be.conf` 沒有
`initial_root_password`（root 帳號只存在 FE metadata），FE 主 task 使用 prestart
已準備好的 `fe.conf`。這個區分只為了不產生沒人讀取的檔案，並非安全邊界：
`my.cnf` 本來就含有同一個明文密碼。

**不能以 hash 取代明文**：`initial_root_password` 只決定首次 bootstrap 的密碼，
之後所有 mysql client 都以 `my.cnf` 的明文登入。也不要在 `fe_config` 自行設定
`initial_root_password`；它屬於 pack 管理的設定，prestart 會拒絕。

## 驗證

在 repository root 執行：

```bash
bash -n docker/runtime/nomad/scripts/prestart.sh docker/runtime/nomad/scripts/consul_ready.sh docker/runtime/nomad/scripts/bootstrap-permit.sh \
  docker/runtime/nomad/scripts/wipe-volumes.sh docker/runtime/nomad/scripts/ops-common.sh
python3 -m unittest discover -s docker/runtime/nomad/tests -v
# 僅 Nomad Pack 0.4.2 以上有 fmt；0.4.1 略過此步驟
nomad-pack fmt -check -recursive docker/runtime/nomad
```

測試會執行真正的 prestart，但以本機 fake mysql 模擬資料庫回應；也會呼叫真正的
`nomad-pack render` 與 `nomad job run -output`，檢查 shell 內容經模板處理後完整保留、
主入口未被覆寫，以及擴容不會改變既有 groups。這些測試不等於真實叢集整合測試。

若本機有 Vault CLI，測試另會啟動只監聽 loopback 的暫時 Vault dev server，使用
合成密碼與真正的 Vault Agent 渲染同一份 templates，驗證 KV v2 讀取、特殊字元跳脫
與原始密碼 bytes。此測試不使用任何既有 Vault address/token/namespace，結束後關閉。
沒有 Vault CLI 時，這兩個整合測試會顯示 skipped。仍未驗證真實 Nomad/Vault JWT
整合或 Doris 4.1.4 叢集啟動。

上線前在測試環境驗證：首次 bootstrap、root 認證、停止原 master 後加入新 FE/BE、
BE allocation 重新排程、全 FE 保留資料卷重啟、以及逐台 image 更新。

參考：[Nomad lifecycle](https://developer.hashicorp.com/nomad/docs/job-specification/lifecycle)、
[restart](https://developer.hashicorp.com/nomad/docs/job-specification/restart)、
[allocation filesystem](https://developer.hashicorp.com/nomad/docs/concepts/filesystem)、
[Vault integration](https://developer.hashicorp.com/nomad/docs/secure/workload-identity/vault)、
[Doris initial_root_password](https://doris.apache.org/docs/3.x/admin-manual/config/fe-config/#initial_root_password)。
