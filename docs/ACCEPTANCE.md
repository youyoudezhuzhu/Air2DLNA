# 真机验收清单（Test 1–12）

对应任务书第 25 节。需要 **iPhone/iPad/Mac + 飞牛 NAS + DLNA 音响在同一 LAN**。

## 准备

```bash
appcenter-cli install-fpk Air2DLNA-1.0.4.fpk --volume 1
appcenter-cli start air2dlna
```

在 Web UI（`http://<NAS_IP>:8788`）里：

1. 把「AirPlay 音箱名称」改成便于识别的名字，例如 **`客厅音响`**，保存。
2. 点「搜索设备」。
3. 在列表里选中你的 DLNA 音响（注意：这里显示的是**音响自己的**名称，
   与上面的 AirPlay 名称互相独立）。
4. 确认「当前播放」区域出现，DLNA 设备显示 **在线**。

记录三个 MAC/名称以便区分：

| 项 | 值 |
|---|---|
| AirPlay 名称（本应用） | 例如 `客厅音响` |
| DLNA Renderer 名称 | 例如 `Living Room Speaker` |
| NAS IP | 例如 `192.168.1.50` |

---

## 自动化前置检查（建议先跑）

```bash
# 1) AirPlay 2 服务记录（关键：必须出现 _airplay._tcp 且带 features / pk）
avahi-browse -rt _airplay._tcp
#   期望：名称 = 你设置的名字；TXT 含 features=0x...、pk=...、protovers=1.1、deviceid=...

# 2) Classic 兼容记录（可选）
avahi-browse -t _raop._tcp
#   期望：出现 "<MAC>@<你的名字>"

# 3) PTP 端口已在监听（AirPlay 2 timing 依赖）
ss -lunp | grep -E ':319|:320'

# 4) Web UI / API
curl -s http://127.0.0.1:8788/api/health          # -> {"ok": true}
curl -s http://127.0.0.1:8788/api/status | head -c 400

# 5) 日志无异常
grep -cE "Traceback" /vol1/@appdata/air2dlna/bridge.log   # -> 0
```

---

## Test 1 — AirPlay Discovery

* 在 iPhone 上打开 **控制中心 → 音频卡片 → AirPlay 图标**（或音乐 App 的
  AirPlay 按钮）。
* **期望**：列表中出现 **`客厅音响`**（即你设置的 AirPlay 名称），
  而不是 DLNA 音响自己的名字。
* 反例排查：如果只看到 DLNA 音响、看不到本应用 → 检查 `_airplay._tcp` 是否注册；
  如果看到的是 `AirPlay 1` 语义的行为 → 确认 shairport-sync 构建带 `Avahi`。

- [ ] 通过

## Test 2 — 连接

* 选中该设备。
* **期望**：连接成功、无报错；Web UI「当前播放」状态从 `STOPPED` 变为
  `PLAYING`/`BUFFERING`；日志出现 `AirPlay client connected` 与
  `AirPlay session established`。

- [ ] 通过

## Test 3 — 播放

* 打开 Apple Music 播放一首歌。
* **期望**：DLNA 音响正常出声；Web UI 显示歌名/艺术家/专辑/封面；
  日志出现 `SetAVTransportURI` 与 `Play`。

- [ ] 通过

## Test 4 — Pause

* 在 iPhone 上暂停。
* **期望**：DLNA 音响**停止出声**；Web UI 状态 `PAUSED`；位置进度冻结不再增长；
  日志出现 `Pause`（若该渲染器不支持暂停实时流，会退化为 `Stop` 并在日志说明，
  音响同样停止出声）。

- [ ] 通过

## Test 5 — Resume

* 继续播放。
* **期望**：从**暂停的位置**继续，而不是从头开始，也不是跳到别处；
  Web UI 状态回到 `PLAYING`。

- [ ] 通过

## Test 6 — Seek

* 拖动进度条到明显不同的位置（例如 01:32）。
* **期望**：音响跳到新位置附近继续播放；Web UI 的位置与总时长随之更新；
  日志出现 `SetAVTransportURI`（新 URI）与 `Stop`。
* 允许 1–2 秒的偏差；**不允许**持续偏到几十秒。

- [ ] 通过

## Test 7 — Next / Previous

* 切到下一首 / 上一首。
* **期望**：正确切歌；Web UI 的标题/艺术家/专辑/时长同步更新；
  日志出现新的 `SetAVTransportURI`。

- [ ] 通过

## Test 8 — Volume

* 在 iPhone 上调整 AirPlay 音量。
* **期望**：DLNA 音响音量同步变化；Web UI 的音量百分比跟随变化。
  映射为线性分贝映射：AirPlay `-15 dB → 50%`、`-9 dB → 70%`、`-144 dB → 静音`。
* 注意：音量同步是 **iPhone → DLNA 单向**（AirPlay 接收端无法反向设置发送端音量），
  这是协议决定的。

- [ ] 通过

## Test 9 — Metadata

* Web UI「当前播放」区域。
* **期望**：显示 Title / Artist / Album / Artwork / Position / Duration。
  封面若发送端未提供则显示占位（不报错）。

- [ ] 通过

## Test 10 — 长时间播放（≥30 分钟）

连续播放至少 30 分钟（建议 60 分钟），期间观察：

```bash
# 内存是否持续增长（应大致平稳）
while :; do ps -o rss= -p "$(cat /vol1/@appdata/air2dlna/app.pid)"; sleep 60; done

# CPU 是否异常（正常应在个位数百分比）
top -b -n1 -p "$(cat /vol1/@appdata/air2dlna/app.pid)" | tail -2

# 漂移：比较 Web UI 位置与 iPhone 显示位置的差值是否持续变大
curl -s http://127.0.0.1:8788/api/status | python3 -c "import json,sys;d=json.load(sys.stdin)['playback'];print(d['state'], d['position_ms'], d['duration_ms'])"
```

**期望**：

- [ ] 不断流、不卡顿
- [ ] 播放位置无**持续**漂移（允许有界的小偏差；若超过
      `drift_threshold_ms`（默认 1500 ms），应用会自动重建会话对齐）
- [ ] 内存无持续增长
- [ ] CPU 无持续异常
- [ ] DLNA 状态与 AirPlay 状态一致（暂停/播放/换曲都同步）

## Test 11 — Renderer 断电

* 播放中把 DLNA 音响断电（或断开网络）。
* **期望**：
  - [ ] 应用**不崩溃**（`appcenter-cli status` 仍为 running；日志无 Traceback）
  - [ ] Web UI 把该设备显示为 **离线**，并提示不会自动切换到别的设备
* 音响重新上线后：
  - [ ] SSDP 自动重新发现（设备重新显示在线，或点「搜索设备」后恢复）
  - [ ] 可以重新选择并正常播放

## Test 12 — AirPlay 重连

* 在 iPhone 上断开 AirPlay（切回本机），再重新连接。
* **期望**：
  - [ ] 新会话正常建立
  - [ ] 旧会话的缓冲被清理（日志出现 `音频缓冲换代`；不会串音/续播旧内容）
  - [ ] 可正常播放

---

## 补充边界测试（建议）

- **设备不支持所需 MIME**：选择一台只声明 `http-get:*:*:*` 的设备（本仓库开发环境
  例如部分智能音箱的 DLNA 实现即属此类）。**期望**：日志明确写出
  `Renderer does not support required MIME type ...`，UI 给出提示，**不静默切换格式**。
- **端口占用**：先让另一个 AirPlay 应用（micast / juneix.airplay2）占用 7000，
  再启动本应用。**期望**：启动失败并给出可读原因（写 `TRIM_TEMP_LOGFILE`），
  而不是静默运行一个不可用的服务。
- **重复 start**：连续执行两次 `appcenter-cli start air2dlna`。
  **期望**：不会产生第二个实例。
- **升级保留配置**：改过 AirPlay 名称后升级到新版本。
  **期望**：名称与 DLNA 选择保留。
- **卸载**：分别选择「保留」与「删除」各卸载一次。
  **期望**：与向导选择一致，且不影响其它应用的数据。
