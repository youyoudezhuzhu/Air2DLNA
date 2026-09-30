# Changelog

本文件记录 Air2DLNA 的版本变更。历史版本的完整说明见 `manifest` 的 `changelog`
字段（应用中心展示用）。

---

## 1.0.23 — Virtual Player 架构重构

把原来「AirPlay 事件 → 直接映射 DLNA 动作」的实现，重构为
**AirPlay（Playback Authority）→ Virtual Player（State Coordinator）→ DLNA Output（Output Renderer）**
三层架构。行为目标不变：小爱音箱应当感觉自己连接的是一个正常、稳定、可暂停、
可恢复、可 Seek、可切歌的 DLNA 播放器。

### 新增

* **`virtual_player.VirtualPlayer`** —— 唯一状态协调中心。维护曲目/位置/时长/
  缓冲/时间线/会话，并把控制请求与实际状态严格分开：
  * 实际状态：`IDLE / BUFFERING / PLAYING / PAUSED / SEEKING / TRACK_SWITCHING /
    RECOVERING / STOPPING`；
  * 请求状态：`PLAY_REQUESTED / PAUSE_REQUESTED / SEEK_REQUESTED /
    NEXT_REQUESTED / PREVIOUS_REQUESTED`；
  * 每个请求携带 `request_id` 与 `control_source`（`AIRPLAY` / `DLNA` / `INTERNAL`）。
* **`dlna_output.DLNAOutput`** —— AVTransport 控制 + 连续 HTTP 媒体输出。
  DLNA 播放期间真实 AirPlay PCM 不足时，在**输出层**生成静音继续供给渲染器，
  **绝不主动 EOF / 断开 HTTP 响应**（`stream.StreamSession.continuous_output`）。
  静音超时只通知控制层进入 `RECOVERING`，不断流；只有链路确实不可用时才执行
  「new generation + SetAVTransportURI + Play」恢复原语（作为 fallback，而非正常路径）。
* **`airplay_remote.AirPlayRemoteController`** —— DACP 反向控制与**逐项能力检测**。
  五项能力（play / pause / seek / next / previous）各自如实上报
  `SUPPORTED` / `UNSUPPORTED` / `UNKNOWN`；调用失败或能力不可用时，
  Virtual Player **绝不假装状态已改变**，保持当前状态与当前曲目。
* **`renderer_profile.RendererProfile`** —— 设备差异集中描述，新增
  `xiaomi_s12` 专门 Profile 与 `generic` 保守兜底；按渲染器身份
  （name / model / manufacturer / UDN）自动选择，可用配置项 `renderer_profile` 覆盖。
* **控制回环防护**：由 DLNA 发起、被 AirPlay 回显的状态变化（例如 DLNA Pause →
  AirPlay `paus`）不再重复触发同一条 DLNA 动作。
* **可配置输出缓冲水位**：`minimum_buffer_ms`（默认 400）、`target_buffer_ms`
  （默认 1000）、`maximum_buffer_ms`（默认 1800）；不强制精确 1 秒。
* **DACP 凭据捕获**：元数据管道上的 `daid`（DACP-ID）、`acre`（Active-Remote）、
  `dapo`（远程控制端口）与发送端 IP 会被记录（此前只在文档映射里列出、从未保存）。
* **29 项新单元测试**（`tests/test_virtual_player.py`）：状态机、静音↔真实 PCM
  切换、不断流保证、逐项反向能力检测与不伪造状态、控制回环防护、Renderer Profile。
* **`CHANGELOG.md`**（本文件）。

### 变更

* **`state.BridgeController` 变成组合根**：只负责组装 `VirtualPlayer` /
  `DLNAOutput` / `AirPlayRemoteController` / `RendererProfile`，提供生命周期与
  `status()`，并为历史调用方保留既有属性/方法名（转发）。状态机逻辑已真正移出。
* **移除「把静音写入 AirPlay RingBuffer」的旧行为**：`resume_prebuffer_ms` 不再向
  环形缓冲写静音，恢复空窗统一由 DLNA Output 层填充。AirPlay RingBuffer 与
  Timeline 从此只承载真实 AirPlay 音频（规格第 23、28 节）。
  受影响的既有测试 `test_prebuffer_fills_silence_when_configured` 已按新规格更新为
  `test_prebuffer_silence_stays_in_output_layer`（断言环形缓冲不被污染）。
* `detect`/`AVTransport#Pause` 策略改由 Profile 决定：`generic` 先尝试真正的
  Pause 并保留 URI（规格默认假设）；`xiaomi_s12` 直接使用 Stop（见下）。
* `/api/status` 增加 `virtual_player`（机器状态 / 请求状态）、`renderer_profile`、
  `reverse_control`（逐项能力与凭据）与缓冲水位诊断字段。
* 版本号 `1.0.21` → `1.0.23`。

### `xiaomi_s12` Renderer Profile（字段取值全部来自真机日志实测）

真机 `bridge.log` 统计渲染器上报的 transport state：

```
renderer=STOPPED          3724 次
renderer=PLAYING            60 次
renderer=PAUSED_PLAYBACK     2 次   ← 几乎从不出现
```

典型序列（每次 AirPlay 暂停后）：

```
23:06:59 INFO [state] AirPlay 暂停：向渲染器发送 Pause（沿用当前 DLNA 会话 gen=1）
23:08:09 INFO [state] 诊断: state=PAUSED renderer=STOPPED rel_time=2000 ... http_clients=1
```

据此：

| 字段 | 取值 | 含义 |
|---|---|---|
| `supports_pause` | `False` | 不是「不认 Pause 动作」，而是**不会停留在 PAUSED_PLAYBACK**：调用后自行进入 STOPPED |
| `pause_keeps_uri` | `False` | 暂停后旧 URI 不再有效 |
| `pause_keeps_http` | `False` | 暂停连带丢弃 HTTP 拉流 |
| `resume_requires_reannounce` | `True` | 恢复必须重新 `SetAVTransportURI + Play`（对旧 URI 裸发 `Play` 无效） |
| `reconnect_behavior` | `"none"` | 已验证不会自行重连 HTTP |
| `persistent_stream` | `False` | 不把媒体资源视为跨暂停长期有效 |

对 S12 而言「暂停后恢复需要重新宣告」是该设备的**正常语义**，但该差异只写在
Profile 里，不写死在 Virtual Player 核心（规格第 21 节）。

### 反向控制（DLNA → AirPlay）的真实现状 —— 请勿误读

架构上已完整实现 DACP 反向控制，但**在本环境实测不可用**：

* DACP 需要发送端提供 `acre`（Active-Remote）与 `dapo`（远程控制端口）。
  `dapo` 来自 shairport-sync 自身的 DACP mDNS 监控，即要求**发送端**在局域网广播
  `_dacp._tcp`；实测 `avahi-browse -rt _dacp._tcp` **无任何输出**，
  2 MB 真机日志里从未出现 `daid`/`acre`/`dapo`。
* shairport-sync `AIRPLAY2.md` 第 54 行明确写着：
  `Remote control facilities are not implemented.`
* 因此五项能力通常全部为 `UNKNOWN`（凭据缺失时无法判定），
  Virtual Player 会如实报告并**保持当前状态与曲目**，绝不伪造。

结论：DLNA 端的 Play/Pause/Next/Previous 在当前 AirPlay 2 + iPhone 环境下
**只能表现为渲染器自身的本地行为**，VirtualPlayer 不会伪造状态。README 与日志
中均不得声称「反向控制已可用」。

### 已验证 / 待真机验证

**已在自动化测试中验证**

* 全部 174 项单元测试通过（`python3 -m unittest discover -s tests`）。
* 端到端集成测试 42/42 通过（`python3 tests/integration_test.py`，真实 SSDP +
  真实 SOAP + 真实 HTTP 拉流 + 真实线格式元数据事件）。
* 状态机迁移、请求/状态分离、静音↔真实 PCM 切换、不断流保证、静音不污染环形缓冲、
  逐项反向能力检测、控制回环防护、Profile 选择与覆盖，均由 `tests/test_virtual_player.py`
  覆盖（全部封闭于 127.0.0.1，无长睡眠）。
* 反向控制的 DACP 请求路径与请求头由本地假 DACP HTTP 服务器验证。

**仍需真机（小爱音箱 S12 + iPhone）验证**

* 连续输出在真机恢复/Seek/切歌空窗中的实际听感与恢复时延。
* `xiaomi_s12` Profile 的 Stop/Pause 策略在真机固件上的表现（不同固件版本可能不同）。
* 反向控制需要一台**真的会广播 `_dacp._tcp` 并提供 `acre`** 的发送端；
  在 AirPlay 2 + iPhone 环境下预期仍然不可用。
* 缓冲水位（minimum/target/maximum_buffer_ms）的取值调优。
