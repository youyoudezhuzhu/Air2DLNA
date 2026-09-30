# Changelog

本文件记录 Air2DLNA 的版本变更。历史版本的完整说明见 `manifest` 的 `changelog`
字段（应用中心展示用）。

---

## 1.0.28 — 找到音频接线 bug；SEEK 实验得出结论

### 结果一：发现一处真实的接线 bug（很可能是长期修不好的元凶之一）
`bridge.py` 把音频读取器的 sink 绑成了**裸环形缓冲**：

```python
self._audio_reader = AudioPipeReader(self.audio_fifo, self._ring)   # ← 旧
```

于是 `VirtualPlayer.on_audio_bytes()` —— 在整个代码库里**只有定义、没有任何生产调用点**
（只有测试调用过）—— 是**死代码**。对照：同一处的元数据读取器是**正确**绑到 controller 的：

```python
self._metadata_reader = MetadataPipeReader(
    self.metadata_fifo, self._controller.on_metadata_item)          # ← 正确的写法
```

**两条兜底因此在生产环境从未执行过：**

1. **暂停期间持续收到 PCM ⇒ 判定 AirPlay 其实已恢复。** 这条是 1.0.18 专门为
   「拖动进度条时 AirPlay 完全不发任何事件（既无 pfls/pdis 也无 pres/pbeg），
   只有 PCM 从新位置继续送来」而加的 —— **恰好就是 seek 场景**。
2. **仍在推送 PCM ⇒ 不把 `pend` 当成播放流真正结束。**

修复：`AudioPipeReader(self.audio_fifo, self._controller)`，并为
`VirtualPlayer` / `BridgeController` 补齐 `append()` 与 `byte_rate`
（读取器使用的 sink 协议）。

### 结果二：SEEK 实验（1.0.27）得到了明确结论
保持会话期间，渲染器**从未转入 STOPPED**：

```
22:02:09  诊断: state=PLAYING machine=PLAYING renderer=PLAYING rel_time=12000 http_clients=1
22:02:26  诊断: state=PLAYING machine=PLAYING renderer=PLAYING rel_time=23000 http_clients=1
```

`renderer=PLAYING`、`拉流连接=1`、`rel_time` 从 12s 正常推进到 23s。
**对比旧行为（每次 seek 都会 `渲染器=STOPPED 拉流连接=0`）**，
这证实了两件事：① 「seek 时主动 Stop / 重建会话」确实是个真问题；
② 「保持会话」的方向是正确的。

### 新增决定性诊断：real_pcm / silence 秒数
既然渲染器在拉流、我们也在推数据，但用户仍听不到声音，就必须回答
**「我们喂给它的到底是音频还是静音」**。诊断行新增：

```
real_pcm=12.3s silence=45.1s
```

分别统计真实 AirPlay PCM 字节与静音填充字节。若 `silence` 远大于 `real_pcm`，
说明问题在 AirPlay 侧没有送来 PCM，而不是 DLNA 侧的处理。

### 验证
单元测试 **196/196** 通过，新增 4 项 `AudioIngestWiringTests` 专门锁定
「音频必须经 VirtualPlayer 进入环形缓冲」这条接线，
并验证两条兜底确实可达。

---

## 1.0.27 — SEEK 实验版：seek 时保持 DLNA 会话（单变量 A/B）

按外部分析（ChatGPT）结论，把「seek 后彻底无声」的**头号嫌疑**做成可验证的最小实验。

### 为什么怀疑「主动 Stop + 重建」而不是 AirPlay
1. **seek 从不发 `pfls`/`pdis`**（真机实测计数为 0）。它表现为
   「暂停 → 恢复且曲目位置跳变」，命中的是 `_handle_play` 的 `resume-rebuild` 分支。
2. 该分支的旧行为是 **Stop + 新 generation + 新 URI + `SetAVTransportURI`** ——
   也就是**主动拆掉渲染器正在使用的 HTTP 连接**，逼它重建整个播放生命周期。
3. 而 AirPlay 此刻要 **10~20 秒**才重新送出 PCM。渲染器在这段空窗里建好即空转，
   随后自行 `STOPPED`（日志：`真实 PCM 恢复` 之后紧接着 `渲染器=STOPPED 拉流连接=0`）。

结论：**「AirPlay 没有 PCM」和「S12 停止播放」之间，我们主动插入了拆连接与重建。**

### 实验内容（只改这一个变量）
```
Seek
 └─ 只 flush 旧 PCM（新 generation）
    保持 DLNA 会话与 URI 不变      ← 不 Stop、不 SetAVTransportURI、不换 URI
    渲染器**现有的** HTTP 连接自动跟随到新 generation
    空窗期间由输出层连续发送静音
    PCM 到达后同一连接无缝继续输出
```
* 保持窗口 **30 秒**；窗口内「静音超时」不再触发 `RECOVERING`/换代
  （否则 8 秒就会打断 30 秒的等待，实验自变量被破坏）。
* 新增配置 `seek_keep_session`（默认 `true`）。设为 `false` 可**一键回到旧行为**做对照。

### 新增 T0~T9 打点（判定「谁先死」）
| 打点 | 含义 |
|---|---|
| T0 | seek 检测 |
| T1 | 是否发送 `AVTransport#Stop`（实验中应为「未发送」） |
| T2 | 是否发送 `SetAVTransportURI`（实验中应为「未发送」） |
| T4 | 渲染器发起 HTTP GET |
| T5 | 首个静音字节 |
| **T6** | **首个真实 PCM 字节** |
| T7 | HTTP 连接被关闭 |
| T8 | 渲染器 TransportState → STOPPED |
| T9 | 保持窗口结束 |

报告末尾会显式给出两个关键间隔：

* **T6→T7**：如果是我们先关闭了连接 ⇒ 本方的连接生命周期问题；
* **T6→T8**：如果是渲染器先转入 STOPPED ⇒ 设备对流媒体的兼容性限制。

### stream 层修正
跟随新 generation 时**重置本连接的字节预算**。同一 HTTP 连接跨代输出时，
旧实现会按旧代的 `total_bytes` 提前结束连接（因为 seek 实验正是让连接跨代存活，
这个 bug 会直接破坏实验）。

### 验证
* 单元测试 **192/192** 通过，含实验开关**两侧**的用例
  （`test_renderer_stopped_keeps_session_when_experiment_on` /
  `..._forces_rebuild_when_experiment_off`），确保回退路径可用。
* `tests/test_playback_lifecycle.py` 的 `RebuildSourceTests.test_seek_rebuilds_exactly_once`
  按实验改为 `test_seek_holds_session_instead_of_rebuilding`。

### 如何判读结果
* **实验成功**（seek → 十几秒静音 → PCM → 正常出声）⇒ 坐实根因是
  「在 AirPlay 无 PCM 的窗口里主动重建了 S12 承受不住的 DLNA 播放生命周期」。
* **实验失败**（静音 → PCM → 仍 `STOPPED`）⇒ 看 T6→T8：若极短，说明 S12 拿到 PCM
  之后仍主动结束会话，属设备兼容性；下一步应做 **WAV vs L16**、`Content-Length`、
  chunked、DIDL 等**独立单变量**对照（不要与本实验混在同一个版本里）。

---

## 1.0.25 — 修复「拖动进度条后完全没有声音」

1.0.24 解决了暂停/恢复延迟（5~6 秒 → 约 3 秒），但**拖进度条后无声**依然存在。根因已定位：

### 根因：沿用 DLNA 会话时从不检查渲染器是否还能播放
`_can_reuse_generation()` 决定 `pbeg` 时是否沿用当前 DLNA 会话，但它只检查
Virtual Player **自己**的状态和会话的 `closed` 标志，**从不检查渲染器是否真的还在播放、
是否还有 HTTP 客户端在拉流**，也**从不查询 Renderer Profile**。真机日志（1.0.24）：

```
21:26:33 pbeg：曲目位置连续（变化 0 ms），沿用当前 DLNA 会话
21:26:33 pbeg：沿用当前 DLNA 会话 gen=4（不重建、不重设 URI）
21:26:34 连续输出静音超时：进入 RECOVERING（渲染器=STOPPED 拉流连接=0）
```

渲染器已经是 `STOPPED`、**拉流连接 = 0**，我们却「沿用会话」——既不
`SetAVTransportURI` 也不 `Play`。**没有任何人会去播放**，音箱因此永远无声。

拖进度条时 iPhone 会先发 `pend`、再重建 AirPlay 会话，这段空窗里渲染器必然已经
STOPPED，所以**每次拖进度条都会命中**这条路径。这也解释了为什么它看起来"架构改了
却没修好"：问题不在架构，而在这个判定漏了最关键的一个条件。

### 修复：补两道闸
1. **Renderer Profile 一票否决** —— `resume_requires_reannounce` 为真时一律不沿用会话。
   真机 S12 该值为 `True`，但此前它是**死字段**：全部代码里没有任何地方读过它，
   所以 Profile 里写着"恢复必须重新宣告"，实际却从来没生效过。
2. **渲染器必须真的能继续播放** —— 要求 `renderer_state != STOPPED` 且
   `session.clients > 0`（确实有客户端在拉流），否则「沿用」等于没人播。

同时保留**反向对照测试**：渲染器在播且有客户端、位置连续时，沿用仍必须被允许。
这是为了防止把 `_can_reuse_generation` 简单改成"永远返回 False"这种假修复——
那会让每次 `pbeg` 都换代，音箱重复缓冲，回到 1.0.7 修过的卡顿问题。

### 验证
* 单元测试 **186/186** 通过（新增 4 项 `SeekReannounceTests`）。
* 端到端集成测试 **42/42** 通过。
* `tests/test_playback_lifecycle.py` 中 2 个既有测试补上了前置条件
  （「沿用」的前提是渲染器可继续播放；此前的 fixture 里该前提从未被建立，
  所以它们测的是"位置连续就沿用"，而没有覆盖"渲染器已停止"的情况）。

### 待真机复验
拖进度条的出声与位置正确性。注意：seek 之后 iPhone 需要约 15~17 秒重建 AirPlay
会话并重新缓冲（真机日志实测），这段空窗由输出层静音填充，属于**发送端行为**，
不在本应用可控范围内；本版保证的是空窗结束、PCM 到达后能正确地重新宣告并出声。

---

## 1.0.24 — 修复真机反馈：暂停/恢复延迟与拖进度条后无声

真机（小爱音箱 S12 + iPhone AirPlay 2）反馈两个问题：**暂停/恢复各有 5~6 秒延迟**、
**拖动进度条后完全没有声音**。共定位并修复四处缺陷，全部有真机日志或代码证据。

### ① 换代后旧 token 只拿到 0 字节（无声的主因）
`new_generation()` 会把旧会话标记 `closed`，而 HTTP 服务见到 `closed` 就断开连接。
但渲染器往往**仍在拉这个旧 URL** —— 音箱不会因为我们换了 URI 就立刻放弃旧连接。
真机日志（gen=3 已开始后）：

```
渲染器开始拉流: token=1790760602-2 gen=2 range_start=10138872 (第 2 次连接)
渲染器拉流结束: token=1790760602-2 本次连接 0.0s
渲染器开始拉流: token=1790760602-2 gen=2 range_start=44 (第 3 次连接)
```

`本次连接 0.0s` 就是 0 字节。现在把 `closed` 分成两种含义：

* **被换代取代** —— 跟随最新一代继续输出（实时流没有「旧版本」可言）；
* **被主动关闭**（Stop / close_all）—— 正常结束连接。

同时修掉一个时序陷阱：`ring.flush()` 先发生，连接可能已跟随到新代，之后
`new_generation()` 才置上 `closed`，此时 `session.generation == ring.generation`，
只比较 generation 会漏判 —— 改用「是否仍是 current 会话」判定。

### ② 预滚动期间换代中断整轮收敛（无声的直接原因）
`do_play()` 等待预滚动时，`ring.wait_for_data()` 抛出的 `StaleGeneration` 没有被捕获，
异常冲出 `converge`，导致 `SetAVTransportURI` / `Play` **根本没有发出去**。
seek 会连续产生多代（`pdis` 一代 + `pbeg`/`prsm` 一代），所以几乎必然触发。
真机日志原文：

```
ERROR [dlna_output] DLNA 收敛过程异常
  ...
  File "dlna_output.py", line 427, in do_play
    if not self.ring.wait_for_data(0, generation, needed, timeout=8.0):
air2dlna.ringbuffer.StaleGeneration: 等待预滚动期间缓冲换代
```

现在捕获该异常并把意图重新指向最新一代，让收敛线程再跑一轮。

### ③ Renderer Profile 选择结果被按 UDN 永久缓存（延迟的主因）
真机日志：

```
Renderer Profile 选择: udn=uuid:64f2215e-... name='小爱音箱-2284' model='' -> generic
```

SSDP 阶段只有名字、`model` 还是空串，而 `model='S12'` 要等设备描述 XML 抓回来才知道。
旧缓存键只有 UDN，于是 **generic 被缓存整个进程生命周期**，`xiaomi_s12` Profile
（以及它的暂停语义）永远不生效 —— 这也是升级到 1.0.23 后体感没有改善的原因。
现在缓存键包含 `model`/`name`/`manufacturer`/`override`，身份变好立即失效重算。

### ④ 对「暂停实为 Stop」的设备仍启用 keepalive（延迟的直接原因）
keepalive 的策略是**不对渲染器发任何 UPnP 命令**、只在输出层改送静音。对小爱 S12
（真机日志 `renderer=STOPPED` 3724 次 vs `PAUSED_PLAYBACK` 2 次，暂停即 Stop 并丢弃 HTTP）：
音箱要先把已缓冲的真实 PCM 放完（约 5~6 秒）暂停才生效，恢复时又要把缓冲里的静音
放完（约 5~6 秒）才听到声音 —— 正是「暂停/恢复各延迟 5~6 秒」。
现在 `RendererProfile.supports_pause is False` 时**一票否决 keepalive**，自动改用
`current`（真实 Pause/Stop + 恢复时重新宣告）。

### 验证
* 单元测试 **182/182** 通过（1.0.23 为 174，新增 8 项 `tests/test_s12_fixes.py`）。
* 端到端集成测试 **42/42** 通过。
* `tests/test_playback_lifecycle.py` 的 keepalive **机制**测试改用允许 keepalive 的
  通用设备（原先照抄了 `model="S12"`，会让机制测试跑到不适用的设备上；原因已写在
  测试注释里）。S12 的 keepalive 否决有专门用例覆盖。

### 待真机复验
暂停/恢复的实际时延、拖进度条的出声与位置正确性、以及 `silence_timeout_seconds`（默认 8s）
是否合适。

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
