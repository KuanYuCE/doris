# FE 半初始化狀態的偵測與恢復

狀態：**已實作**（`scripts/prestart.sh`）。操作說明在 README 的「重啟與 master 切換」
與「只重置一台 FE」；本文記錄根因與設計取捨。

## 1. 問題

部署時 follower 反覆出現：

```
finished to get cluster id: ..., isElectable: true, role: FOLLOWER and node name: fe_...
image does not exist: .../doris-meta/image/image.0
begin to transfer FE type from INIT to MASTER
current node <follower-ip>:9010 is not added to the cluster, will exit.
```

master 的 `SHOW FRONTENDS` 顯示該 follower `Join=true, Alive=false`。只能清空 volume 才能恢復。

## 2. 根因（依原始碼）

| 位置 | 行為 |
| --- | --- |
| `Env.getClusterIdAndRole` helper 分支 | 向 helper `/role` 查到角色後**先寫 ROLE**，再下載 VERSION、再 `getNewImage`（任何 HTTP 失敗都 `throw` → 行程退出） |
| `BDBEnvironment.setup` | 之後才加入 BDB group；握手時檢查時鐘差（`max_bdbje_clock_delta_ms`，5 秒），失敗 3 次 `System.exit(-1)` |
| `Env.getClusterIdAndRole` 本地分支 | 本地有 ROLE+VERSION 時忽略 helper 的角色資訊；**沒有 `--helper` 時以自己為 helper** |
| BDB JE | 空的 env 以自己為 helper 開啟時**建立新的 replication group**；以其他節點為 helper 時只會嘗試加入 |
| `Env.transferToMaster` → `checkCurrentNodeExist` | 重播空 journal 後找不到自己 → `System.exit(-1)` |
| `Env.addFrontend` + `BDBHA.addUnReadyElectableNode` | `ALTER SYSTEM ADD FOLLOWER` 時 master 以 `ElectableGroupSizeOverride` 把尚未加入的節點排除在 quorum 之外，直到第一次 heartbeat OK。所以**多台同時加入不會讓 master 失去 quorum** |
| `init_fe.sh` | `FE_MASTER_IP == FE_CURRENT_IP` → 不帶 `--helper` 啟動；否則 `--helper $FE_MASTER_IP:9010`。未註冊就啟動時 FE 只會每 5 秒重試 `/role`，不會寫 ROLE、不會退出 |

鏈路：follower 第一次以 `--helper master` 啟動，寫完 ROLE+VERSION 後在加入 BDB 前中斷
（這次是時鐘差 16 秒）→ 舊版 prestart 看到 ROLE+VERSION 就把 `FE_MASTER_IP` 設為自己
→ 不帶 `--helper` → 自建一個只有自己的 group → 當選 master → journal 裡沒有自己 → 退出
→ `restart.attempts = 0` 換新 allocation → 重複。一次中斷變成永久失敗；而且自建 group
之後 `bdb/` 已和真正的叢集不相容，只剩清理一途。

## 3. 設計

原則：**不清理、不加 lock、不改 Doris image**。半加入的節點只缺一個正確的 helper。

既有 FE（本地有 ROLE 或 VERSION）：

1. 在 `existing_fe_discovery_timeout`（30 秒）內找現任 master。
2. 找到且不是自己：確認 master 的名單列有自己（否則明確失敗）、比對時鐘，
   `FE_MASTER_IP = master`。健康成員沿用本地 group；半加入節點接續加入；只有 ROLE
   的節點走 helper 分支補下載 VERSION 與 image。
3. master 是自己：`FE_MASTER_IP = 自己`。
4. 逾時：`FE_MASTER_IP = 第一個不是自己的 seed`；單 FE 叢集才用自己。健康成員照常
   冷啟動選舉；半加入節點因 helper 不是自己而無法自建 group，逾時退出、等下次排程。

新 FE：找到 master 後先比對時鐘（`max_clock_skew_seconds`，4 秒），超過就在寫入任何
metadata 之前失敗，原因直接出現在 allocation log。

逾時值不是安全關鍵：master 活著卻暫時沒查到時，健康 FE 以 peer 為 helper 正常啟動，
半加入 FE 再失敗一次、下一輪再找。

## 4. 刻意不做的事

- **自動清理 `image/`、`bdb/`**：需要 prestart 推斷「這份 metadata 是半成品」，推斷錯
  就刪掉健康成員的 metadata。改用正確 helper 讓 Doris 自己接續，走的是和新節點相同的
  程式路徑，冪等。
- **`.bootstrap-pending` 與自動重新 bootstrap**：bootstrap FE 在 VERSION 寫入與
  `logAddFirstFrontend` 之間被中斷的窗口只有幾秒、只在全新叢集發生，而 prestart 無法
  區分它和「健康叢集冷啟動、這台是 quorum 必需」。維持明確失敗，README 記錄特徵與處置。
- **跨 group 的啟動順序或 lock**：`update.max_parallel = 1` 只限制單一 group；Nomad 沒有
  跨 group 排序。方案實作後，同時更新所有 FE 的後果從永久失敗降為多幾輪 reschedule；
  README 已要求 FE 設定變更一次一台。

## 5. 驗證

- `tests/test_prestart.py`：既有 FE 以 master／自己／peer 為 helper、只有 ROLE 的恢復、
  不在名單則失敗、時鐘差過大則失敗且不寫 endpoint、單 FE 用自己、冷啟動不等完整
  `discovery_timeout`。
- `tests/test_pack.py`：新變數只傳入 FE 的 prepare task。
- 未在真實 Doris 與 Nomad 上驗證。上線前在測試環境確認：(1) 三台健康 FE 全部以
  `--helper <peer>` 冷啟動能選出 master；(2) 半加入的 FE 以 `--helper master` 啟動能
  完成加入；(3) 半加入的 FE 以 `--helper <連不上的 peer>` 啟動會退出而不是自建 group。
