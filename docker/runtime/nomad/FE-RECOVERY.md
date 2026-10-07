# FE 半初始化狀態的偵測與自動恢復（設計）

狀態：**待確認，尚未實作**。確認後依「變更清單」實作，並把結果整理進 README 與 PLAN.md。

## 1. 問題

部署時 FE 反覆出現以下錯誤，只能清空 volume 才能恢復：

```
finished to get cluster id: ..., isElectable: true, role: FOLLOWER and node name: fe_...
image does not exist: .../doris-meta/image/image.0
begin to transfer FE type from INIT to MASTER
current node 100.11.85.5:9010 is not added to the cluster, will exit.
```

已確認 `100.11.85.5` 是 follower。

### 1.1 Doris 的行為（依原始碼）

| 位置 | 行為 |
| --- | --- |
| `Env.java:1350` | 本地已有 `image/ROLE` 與 `image/VERSION` 時走「本地」分支，**忽略 `--helper` 的角色資訊、不下載 image** |
| `Env.java:1471-1478` | follower 首次以 `--helper` 加入時，先寫 ROLE，再從 helper 下載 VERSION，之後才下載 image（第 1531 行）並加入 BDB group |
| `Env.java:1692` | 沒有 `--helper` 時，以自己作為 helper |
| `Env.java:1366-1430` | 沒有 ROLE/VERSION 的首個節點：寫 ROLE、VERSION，只在記憶體中把自己加入 frontends |
| `Env.java:1792-1797` | 首個 master 在 `transferToMaster` 才 `logAddFirstFrontend`，把自己寫進 journal |
| `Env.java:2241-2258` | `checkCurrentNodeExist`：名單中找不到自己就 `System.exit(-1)` |
| `FrontendsProcNode.java:104-106, 208` | `SHOW FRONTENDS` 的 `Join` = 該節點是否在 BDB replication group 的成員名單中 |
| `BDBHA.java:127-151` | 成員名單取自 `ReplicationGroup.getElectableNodes()`，是**持久的成員資格，不代表存活**；但遇到 `UnknownMasterException` 時回傳**空名單** |

### 1.2 根因

**情況 A：follower 半加入（這次遇到的情況）**

1. follower 第一次 `--helper <master>` 啟動，寫入 ROLE、下載 VERSION 後，在加入 BDB group 之前停掉（被 kill、job 更新、master 剛起來還不穩、master 切換）。
2. 下一個 allocation 的 prestart 看到 ROLE+VERSION，就當成既有 FE，把 `FE_MASTER_IP` 設成自己。原 entrypoint 因此不帶 `--helper` 啟動。
3. FE 以自己為 helper，用空的 BDB 自建一個新 group，被選為 MASTER，重播空 journal，找不到自己而退出。之後每次都一樣。

**情況 B：bootstrap FE 首次啟動中斷**

bootstrap FE 寫入 VERSION 之後、`logAddFirstFrontend` 之前停掉。之後每次啟動都會走 1.1 表格中的「本地」分支，journal 裡沒有自己，同樣在 `checkCurrentNodeExist` 退出。prestart 一樣當成健康的既有 FE 放行。

**共同點**：prestart 只用「ROLE+VERSION 是否存在」判斷 FE 是否完整，分辨不出「加入或 bootstrap 做到一半」的情況。而 `restart.attempts = 0` 讓任何一次中斷都會換新的 allocation，所以只要中斷一次就再也起不來。

## 2. 目標與非目標

目標：
- 正常流程與上述兩種半初始化狀態都不需要人工介入。
- 不會把健康 FE 的 metadata 誤判為半初始化而清掉。
- 全叢集冷啟動時，既有 FE 仍可同時啟動、湊成 quorum。

非目標：
- 不處理 metadata 實際損毀、叢集 quorum 永久遺失、IP 變更等需要 Doris metadata recovery 的情況。這些維持「明確失敗，由操作人員處理」。
- 不修改 Doris image 與 upstream entrypoint。

## 3. 設計

### 3.1 新的標記檔（位於 FE meta volume）

| 檔案 | 誰寫入 | 意義 |
| --- | --- | --- |
| `.bootstrap-approved` | 操作人員（`bootstrap-permit.sh`） | 允許建立新叢集（不變） |
| `.bootstrap-consumed` | prestart | 授權已使用（不變） |
| `.bootstrap-pending` | **新增**：prestart 消耗授權時一併寫入 | bootstrap 已開始，**尚未確認完成** |

`.bootstrap-pending` 由以下兩處移除，兩者都代表「已確認 bootstrap 完成」：
- `consul_ready.sh fe` 判定 FE ready 時（自身 `Join=true`、`Alive=true`，且看得到存活的 master）。主 task 有掛載 meta volume，script check 由 Nomad 在主容器內執行。
- prestart 發現 master 的 `SHOW FRONTENDS` 中自身為 `Join=true` 時。

舊版 pack 建立的叢集不會有 `.bootstrap-pending`，因此不會被新邏輯判為「bootstrap 未完成」。

### 3.2 既有 FE 先找 master，作為 helper（第 1、2 點）

新增 pack 變數 `existing_fe_discovery_timeout`（預設 30 秒），傳入 prestart 的 `EXISTING_FE_DISCOVERY_TIMEOUT`。

已有 ROLE+VERSION、且沒有 `.bootstrap-pending` 的 FE：

1. 在 `EXISTING_FE_DISCOVERY_TIMEOUT` 內，用與新節點相同的方式探索 master（Consul 中健康的 FE 加上 seeds，並向 master 本身再確認一次）。
2. **找到 master M，且 M 是自己** → `FE_MASTER_IP` 設為自己（現行行為）。
3. **找到 master M，且 M 不是自己** → 向 M 查詢 `SHOW FRONTENDS` 中自身那一列（Host = 本機 IP、EditLogPort = 9010）：
   - `Join=true`：健康成員。`FE_MASTER_IP=M`，原 entrypoint 以 `--helper M` 啟動。對健康 FE 沒有影響，BDB 會使用本地的 group 資訊。
   - `Join=false`：半加入的 follower，見 3.3。
   - 沒有這一列：這台 FE 已被移出叢集，或 IP 已變更。**明確失敗**，不啟動（避免自建 group）。
4. **逾時都沒找到 master**（全叢集冷啟動）→ `FE_MASTER_IP` 設為自己（現行行為），讓既有 FE 同時起來湊 quorum。

代價：既有 FE 每次啟動最多多等 `existing_fe_discovery_timeout`。冷啟動時所有 FE 都等這麼久之後才一起啟動，仍然可以湊成 quorum。

### 3.3 半加入的 follower：清掉不完整的 identity，重新加入（情況 A）

條件（**全部成立**才執行）：
- 本地有 ROLE（不論 VERSION 是否存在），且不是 bootstrap 未完成的 bootstrap FE（3.4）。
- 已確認的 master M（不是自己）的 `SHOW FRONTENDS`：
  - **M 自己那一列 `Join=true`**。用來排除 `UnknownMasterException` 造成成員名單為空、所有節點都顯示 `Join=false` 的情況（1.1 表格最後一列）。
  - 自身那一列 `Join=false`。
- 間隔幾秒後再查一次，結果相同（排除 leader 切換中的暫時狀態）。

動作：
1. 把 meta 目錄中的 `image/`、`bdb/` **搬到** `.half-joined-<UTC 時間>/` 備份，不直接刪除。
2. `FE_MASTER_IP=M`。原 entrypoint 看到 meta 為空，就走新節點流程：`check_fe_registered` 發現已註冊，所以不再 `ADD FOLLOWER`，直接 `start_fe.sh --helper M`。Doris 會走完整的 helper 分支，重新取得 ROLE、VERSION 與 image（`Env.java:1440-1531`）。

現行的「只有 ROLE 沒有 VERSION」（目前會報 `Incomplete FE metadata` 並永遠失敗）也適用這個流程：ROLE 已寫、VERSION 還沒下載就中斷，同樣屬於半加入。

### 3.4 bootstrap 未完成的 bootstrap FE：自動重新 bootstrap（第 3 點，情況 B）

條件（**全部成立**才執行）：
- `NODE_IP == BOOTSTRAP_IP`，本地有 ROLE（或 VERSION），且有 `.bootstrap-pending`。
- 在完整的 `DISCOVERY_TIMEOUT`（預設 120 秒）內，**任何地方都看不到 master**。

動作：
1. 把 `image/`、`bdb/` 搬到 `.half-bootstrap-<UTC 時間>/` 備份。
2. 保留 `.bootstrap-consumed` 與 `.bootstrap-pending`，`FE_MASTER_IP` 設為自己，重新執行首次啟動。

若在 discovery 期間**看得到 master**：
- master 列出自己且 `Join=true` → bootstrap 其實已完成，只是標記沒清掉。移除 `.bootstrap-pending`，再照 3.2 處理。
- 其他情況 → 明確失敗，由操作人員判斷。

### 3.5 既有的明確失敗維持不變

- 有 `image/` 或 `bdb/`，卻沒有 ROLE（不是 Doris 寫入順序會產生的狀態）。
- 沒有 metadata 的 FE 被回報為 master（會變成第二個叢集）。
- 新增：既有 FE 不在 master 的名單中（3.2 的第 3 點）。

## 4. 安全性分析

| 風險 | 防範 |
| --- | --- |
| 把健康 follower 誤判為半加入而清掉 | `Join` 是持久的 group 成員資格，停機的成員仍為 `true`；要求 master 自身 `Join=true`（成員名單有效）；查兩次結果相同；清除前先搬到備份目錄 |
| 冷啟動時把健康的 bootstrap FE 誤判為未完成 | 只認 `.bootstrap-pending`（舊叢集沒有）；標記由 ready check 與 prestart 兩處清除；必須在整個 `DISCOVERY_TIMEOUT` 都看不到 master；清除前先備份 |
| `.bootstrap-pending` 一直沒被清掉（例如 sql-ready check 從未通過） | 這時部署也不會變成 healthy，在 Nomad 或 Consul UI 看得到。prestart 在看得到 master 時也會清除。最壞情況：全叢集冷啟動、bootstrap FE 又是湊 quorum 所必需，就可能被重新 bootstrap，但原本的 metadata 會留在備份目錄，可以手動還原 |
| bootstrap 在標記清除前就已讓 follower 加入，之後又重新 bootstrap | 需要 master 已能處理 SQL、且 ready check（每 15 秒）尚未執行就中斷，機率很低。那台 follower 之後會落入「不在 master 名單中」而明確失敗，需要人工清除；全新叢集沒有使用者資料 |

## 5. 變更清單

| 檔案 | 變更 |
| --- | --- |
| `scripts/prestart.sh` | 抽出 master discovery 函式；依第 3 節的狀態表實作既有 FE、半加入 follower、bootstrap 未完成的處理；備份目錄；`.bootstrap-pending` 的寫入與清除 |
| `scripts/consul_ready.sh` | FE ready 時移除 `.bootstrap-pending` |
| `templates/doris.nomad.tpl` | 把 `EXISTING_FE_DISCOVERY_TIMEOUT` 傳入 FE prepare |
| `variables.hcl` | 新增 `existing_fe_discovery_timeout`（預設 30） |
| `tests/test_prestart.py` | 每個分支一個測試（見第 6 節） |
| `tests/test_ready.py` | ready 時移除標記；不 ready 時保留 |
| `tests/test_pack.py` | 新的 env 有傳入 |
| `README.md` | 更新「prestart 與原入口的分工」「重啟與 master 切換」「密碼、資料卷與 bootstrap 的界線」；說明標記檔與備份目錄 |
| `PLAN.md` | 新增這次的變更與驗證紀錄 |

## 6. 測試計畫

`test_prestart.py` 使用 fake mysql，每台 FE 可回傳不同的 `SHOW FRONTENDS`（含 `Join` 欄位）：

1. 既有 FE，找到 master 且自身 `Join=true` → endpoint 為 master，metadata 不變。
2. 既有 FE，master 就是自己 → endpoint 為自己。
3. 既有 FE，冷啟動（找不到 master）→ 在 `EXISTING_FE_DISCOVERY_TIMEOUT` 後以自己為 endpoint。
4. 半加入 follower：兩次都是 `Join=false` 且 master `Join=true` → `image/`、`bdb/` 搬到備份，endpoint 為 master。
5. 只有 ROLE 沒有 VERSION，且 master 顯示 `Join=false` → 同 4。
6. master 自身 `Join=false`（成員名單為空的情況）→ **不清除**，明確失敗。
7. 兩次查詢結果不同 → 不清除。
8. 既有 FE 不在 master 名單中 → 明確失敗，不清除。
9. bootstrap FE 有 `.bootstrap-pending`，找不到 master → 備份後重新 bootstrap，endpoint 為自己。
10. bootstrap FE 有 `.bootstrap-pending`，master 列出自己 `Join=true` → 移除標記，照 3.2 處理。
11. 舊叢集：bootstrap FE 有 `.bootstrap-consumed`、沒有 `.bootstrap-pending`、找不到 master → 照冷啟動處理，**不清除**。
12. 消耗授權時會一併寫入 `.bootstrap-pending`。

另外：`test_ready.py` 測試標記的清除；在 Nomad dev agent 上驗證 pack 可以 render、job 可以註冊。**真實的 Doris FE 半加入情境需要在你的環境驗證**，這裡沒有 Doris image。

## 7. 待確認

1. `existing_fe_discovery_timeout` 預設 30 秒是否可以接受（既有 FE 每次啟動最多多等這麼久）。
2. 備份目錄留在 meta volume 內，不會自動刪除。是否要改成直接刪除，或保留最近 N 份。
3. 3.4 的自動重新 bootstrap 是否接受第 4 節最後兩列的殘餘風險；若不接受，改為「明確失敗並提示用 `wipe-volumes.sh`」。
