# Doris on Nomad（Docker driver）

這是一個保留官方 image 主程序 entrypoint 的 Nomad Pack 範例。首次部署與擴容
使用同一套 pack；FE/BE groups 同時排程，由各自的 `prestart` 等待必要條件。
不修改 `docker/runtime` 原有腳本，也不另建 Doris image。

## 檔案與啟動流程

- `templates/doris.nomad.tpl`：每個固定節點一個 group，包含 `prepare` 和 `doris` tasks。
- `templates/_credentials.tpl`：透過 Vault KV v2 與 Nomad HCL `template` blocks 產生權限 `0600` 的 `.my.cnf`。
- `templates/_fe-config.tpl`、`templates/_be-config.tpl`：獨立的 FE／BE 設定片段 templates。
- `scripts/prestart.sh`：在對應的 Doris image 內準備設定、探索 master、註冊新成員。
- `scripts/credentials.py`：僅供選用的 `credential_source="nomad"` 模式使用，不是 Vault 模式的必要步驟。
- `examples/cluster.hcl`：3 FE + 3 BE 的 pack 變數。
- `examples/client.hcl`：每台 Nomad client 所需的持久化 host volumes。

```text
prepare task (prestart, sidecar=false)
  ├─ 複製該 image 的 conf 到 /alloc/data/conf
  ├─ FE 有 ROLE + VERSION → 直接放行，不等 SQL/master
  ├─ 新 FE / BE → 輪詢 discovery seeds → SHOW FRONTENDS 找現任 master
  │                                      → 檢查/新增 membership
  └─ 寫入 /alloc/data/endpoint.env
                ↓
doris task
  ├─ BASH_ENV=/alloc/data/endpoint.env
  ├─ /root/.my.cnf ← 該 task 的 secrets/my.cnf
  ├─ /opt/apache-doris/{fe,be}/conf ← prestart 複製的設定
  └─ 原本 image 的 ENTRYPOINT（不覆寫 command / args / entrypoint）
```

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
- IP 必須確實屬於該 host，且所在 `/24` 只能對應一個適用的介面位址。
  原有 entrypoint 會自行附加 `/24` 的 `priority_networks`，本 pack 不修改它。
- Nomad client 已配置持久化 host volumes；資料不能放在 ephemeral allocation 目錄。
- 範例 FE memory 為 16 GiB，以容納 image 常見的 8 GiB JVM heap；請對照實際 image
  與主機容量配置資源。FE 與 BE 同機時，兩者 reservation 必須都能滿足。
- 依 Doris 正式部署要求預先設定主機，例如 BE 的 `vm.max_map_count`、磁碟、時鐘同步等。

本地驗證工具版本：Nomad Pack 0.4.2、Nomad 2.0.4。範例 image tag 為使用者指定的
`apache/doris:fe-4.1.4`／`apache/doris:be-4.1.4`；尚未對這兩個發行 image 執行叢集整合測試。
上線前先檢查 image 的 entrypoint、工具、設定及實際 SQL 輸出；正式使用建議固定 digest。

## 一次性準備

1. 在每台指定 client 建立並掛載 `/srv/doris/fe-meta`、`/srv/doris/be-storage`。
   將 `examples/client.hcl` 的 host volumes 合併到 Nomad client 設定。這些路徑必須位於
   正確的持久磁碟，不能在磁碟缺失時退回空目錄。
2. 複製 `examples/cluster.hcl` 到你自己的設定檔，填入 client name、IP、image、volume。
   `bootstrap_fe` 是首次建立叢集的唯一 FE，不代表永久 master。
   `discovery_fe_ips` 是穩定的 discovery seeds，建議為最初三台 FE。
3. 使用既有的 Vault KV v2 secret：mount 為 `kv-data`，secret 路徑為
   `doris-secret/bootstrap`，欄位為 `password`。pack 變數如下：

   ```hcl
   credential_source  = "vault"
   vault_role         = "doris"
   vault_secret_path  = "kv-data/data/doris-secret/bootstrap"
   vault_password_key = "password"
   ```

   `vault_secret_path` 是 **API 路徑**，所以 KV v2 的 mount 後需要 `/data/`。
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
   `fe.conf` 的 `initial_root_password`。Vault 不需要另外儲存 hash。
   第一次建立叢集就使用這組密碼，不需等 FE 起來後再執行 `SET PASSWORD`。

   Vault token 不注入容器的環境變數或檔案；Nomad 管理 token 與 template 渲染。
   這是固定 KV root 密碼的流程，不是 Vault database engine 的動態帳號流程。

4. **僅限確定為全新叢集**：在 `bootstrap_fe` 對應的 host 上，於空白 FE 資料卷建立
   一次性授權檔：

   ```bash
   sudo touch /srv/doris/fe-meta/.bootstrap-approved
   ```

   其他 FE 不建立此檔。prestart 先嘗試發現既有 master；逾時且沒有看到 master，
   才消耗這份授權並允許初始化。這個操作是磁碟初次佈署的一部分，不需另一套 pack。

## 部署

```bash
nomad-pack render docker/runtime/nomad -f /path/to/cluster.hcl \
  --to-dir /tmp/doris-render --auto-approve
nomad job validate /tmp/doris-render/doris/doris.nomad
nomad-pack plan docker/runtime/nomad -f /path/to/cluster.hcl --name doris
nomad-pack run docker/runtime/nomad -f /path/to/cluster.hcl --name doris
```

只需執行一個 pack。第一次 bootstrap 預設會先探測 120 秒；其他 allocation
可能先逾時而被重新排程，之後會加入已建立的叢集。Doris 初始化和 quorum 恢復
需要時間，請查看 `prepare` 日誌，而非只看容器是否立即出現。

```bash
nomad job status doris
nomad alloc logs <allocation-id> prepare
nomad alloc logs <allocation-id> doris
```

Nomad 的服務檢查為 TCP listener，**不代表 metadata 已追平、quorum 可寫或 BE
所有 tablet 都健康**。以已認證的 SQL 執行 `SHOW FRONTENDS`、`SHOW BACKENDS`
驗證所有預期成員、Alive、master 與 metadata 追趕狀態後，再接受流量或更新下一台。

## 重啟與 master 切換

既有 FE 的 `ROLE`、`VERSION` 都存在時，prestart 不等待 master，直接放行，讓 FEs
同時啟動並自行選舉。缺失部分 identity files 或已有不完整 metadata 時會退出，
不把它當作新叢集。

BE 每次新 allocation 都重新探索 master。prestart 已替新 BE 註冊 membership，
但原有 BE entrypoint 的 status check 仍保留：FE 若在此時故障，主 task 仍可能退出。
這版靠重新排程，再跑一次 discovery 收斂，並不承諾啟動過程完全沒有重試。

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

新增 FE／BE 時，只在同一份 `cluster.hcl` 對應 list 增加一個 map，預先準備
該 host volume，再執行同樣的 `plan`／`run`。**不要同時修改 `discovery_fe_ips`**：
把完整節點清單注入每個既有 group 會造成配置改變，使擴容變成全叢集更新。
Seeds 只需有一台可連線且能回報現任 master；master 本身不必在 seed 清單。
所有 seeds 都不可用時，新節點加入會等待，既有 FE 仍可啟動。

FE 建議維持適當的奇數個 electable members；本範例新增 FE 一律為 FOLLOWER。
不能藉由刪除 list entry 安全縮容：BE 要先 decommission，FE 要依 Doris membership
維護程序移除。本 pack 不自動 DROP 成員或刪除資料。

每個節點有自己的 `image`，升級時一次只改一個 FE 的 image，plan 應只更換那個
FE group；確認 SQL 健康後再更新下一台。`max_parallel=1` 是 **每個 group** 的限制，
並不保證不同 FE groups 依序更新。修改共用模板、資源參數或 seed 清單也可能影響
所有 groups；不要直接把這類更新套到正式叢集，需安排逐組 rollout 或維護停機。

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

若不用 Vault，可改成 `credential_source="nomad"`，再執行：

```bash
python3 docker/runtime/nomad/scripts/credentials.py --job doris --namespace default
```

此備用模式會透過 stdin 建立 `nomad/jobs/doris` 的 `my_cnf` 與 `root_password_hash`
items，使用 CAS=0 拒絕覆寫既有 variable。使用 Vault 時不需要執行這個工具。

## 驗證

在 repository root 執行：

```bash
bash -n docker/runtime/nomad/scripts/prestart.sh
python3 -m unittest discover -s docker/runtime/nomad/tests -v
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
