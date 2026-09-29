# TECHNICAL_DESIGN.md

**项目**：Air2DLNA 音频桥接（飞牛 OS 原生 FPK 应用）
**目标产物**：`Air2DLNA-1.0.1.fpk`
**约束**：禁止 Docker / Podman / LXC / 容器套容器，必须原生进程运行在飞牛 OS 主机
**文档状态**：编码前的技术评估（本文档为实现的规格来源）

---

## 0. 结论摘要

| 决策项 | 选择 |
|---|---|
| AirPlay 2 接收器 | **shairport-sync 5.5.1**（AirPlay 2 模式），自行编译，静态链接最小化 FFmpeg |
| AirPlay 2 时钟 | **NQPTP 1.2.8**（shairport-sync 官方配套 PTP 守护进程） |
| 音频出口 | shairport-sync 的 **`pipe` 后端**（PCM 到 FIFO）+ **`metadata` 管道**（控制/时序/元数据事件） |
| DLNA/UPnP | **自行实现**（Python 标准库）：SSDP + 设备描述解析 + SOAP + GENA |
| 桥接守护进程 | **Python 3.12**（仅标准库，零第三方依赖） |
| Web UI | 原生 HTML/CSS/JS，由桥接进程自身 HTTP 端口提供 |
| 实时流 | 桥接进程内的 PCM 环形缓冲 + HTTP `audio/wav` / `audio/L16` 实时流出 |
| 打包 | 官方 `fnpack 1.2.4` → `.fpk` |

**为什么不是 Docker**：全部组件编译为 NAS 上的原生 x86-64 可执行文件，由 FPK 的
`cmd/main` 直接以进程方式启动，运行时不依赖 Docker、不依赖开发环境、不依赖 pip/npm 联网。

---

## 1. 最终选择的 AirPlay 2 Receiver 实现

**shairport-sync 5.5.1**（https://github.com/mikebrady/shairport-sync），以
`--with-airplay-2` 构建。

它提供：

- Bonjour/mDNS 服务注册（内置 `tinysvcmdns`，无需系统 Avahi / D-Bus）
- 真正的 AirPlay 2 协议：`HomeKit/SRP` 配对（`pair_ap/`）、`AirPlay 2 Service`（`_airplay._tcp`）
- Realtime Audio 与 Buffered Audio 两种流
- 音频解密（`--with-ssl=openssl`）、RTP/RTCP、timing、buffer
- ALAC（内置解码器）+ AAC（FFmpeg 解码器）
- 播放状态机、音量、元数据、以及可选的 AirPlay 1（Classic，RAOP）回退

版本为 `5.5.1`，构建产物自报：
`01078ad-AirPlay2-smi10-OpenSSL-tinysvcmdns-stdout-pipe-soxr-metadata`

### 为什么不用 openairplay/airplay2-receiver

`openairplay/airplay2-receiver` 是 AirPlay 2 协议的**研究性逆向实现**，成熟度和
长期维护性明显低于 shairport-sync；它在配对、重连、缓冲与多格式兼容上覆盖不足。
任务书第 4 节明确禁止“为看起来完成而重新实现一套不成熟的 AirPlay 2 加密/认证协议”。
因此本项目**不自行实现任何 AirPlay 2 加密/配对逻辑**，全部交由 shairport-sync。

### 关于参考项目 C / D / E

- **AirConnect**：方向是“AirPlay 1 接收 → UPnP 播放”，不是本项目方向，但它是
  DLNA/UPnP MediaRenderer 控制的成熟参考，其 SSDP / SOAP / WAV 虚拟流的工程做法
  被本项目借鉴（详见第 2、8 节）。
- **PhairPlay**：把 AirPlay 与 DLNA 放在同一项目中的组织方式有参考价值，但它不提供
  比 shairport-sync 更强的 AirPlay 2 实现。
- **Waaper / airplay-dlna-bridge**：为**闭源商业产品**，无可用源码与明确许可证，
  无法作为依赖，也无法审计其实现成熟度。本项目仅把它当作“该链路在工程上可行”的
  存在性证据，不引用其任何代码。

---

## 2. 最终选择的 DLNA/UPnP 实现

**自行实现**，使用 Python 标准库（`socket` / `http.client` / `xml.etree` / `http.server` /
`threading`），覆盖：

- **SSDP** 发现 UPnP `MediaRenderer`
- **UPnP Device Description** 解析（friendlyName / UDN / manufacturer / modelName / serviceList）
- **SOAP** 控制：`AVTransport`、`RenderingControl`、`ConnectionManager`
- **GENA** 事件订阅（优先）+ 低频轮询（回退）

### 为什么自行实现

1. **AirConnect 是 C，且以“一个进程一个设备 + 硬编码设备适配”为架构**，无法直接作为库嵌入；
   它的设备适配表（Denon、Sonos、Bose 等）是通过大量 `if (device)` 分支实现的，与
   本项目“一个桥接器 + 可选多 Renderer”的架构不匹配。
2. 本项目**不需要** AirConnect 的全套设备怪癖兼容层就能覆盖标准
   `MediaRenderer:1` 设备（第一阶段）。
3. Python 标准库足够，**零第三方依赖**意味着 FPK 安装时无需联网、无需 pip、无版本漂移。
4. DLNA 渲染器控制在协议层是稳定的（UPnP AVTransport:1 / RenderingControl:1 已冻结多年），
   自行实现的代码量与风险可控。

> 注：AirConnect 的具体技术做法（SSDP 报文、SOAP 动作体、WAV 头构造、Volume 映射）
> 已作为设计输入核对，见第 7、8、10、12 节。

---

## 3. 哪些代码直接复用，哪些自行实现

### 直接复用（外部项目，原样使用 / 仅编译配置）

| 组件 | 来源 | 许可证 | 复用方式 |
|---|---|---|---|
| shairport-sync 5.5.1 | mikebrady/shairport-sync | MIT / GPL-2.0（按组件） | 编译为原生二进制，作为子进程 |
| NQPTP 1.2.8 | mikebrady/nqptp | MIT | 编译为原生二进制，作为子进程 |
| FFmpeg（最小化静态） | FFmpeg n7.1 | LGPL-2.1+ | **仅** AAC 解码器，静态链接进 shairport-sync |
| libconfig / libsoxr / libplist / libpopt / libsodium / libgcrypt | Debian 12 | 各自开源许可 | 随 FPK 附带 `.so`（`app/server/lib`） |

**不自行实现**：AirPlay 2 协议、配对（SRP/HomeKit）、音频解密、RTP/RTCP、
PTP 时钟、ALAC/AAC 解码。这些全部由 shairport-sync + NQPTP 承担。

### 自行实现（本项目原创）

| 模块 | 职责 |
|---|---|
| `netif.py` | 网络接口探测与 LAN 接口择优（第 17 节） |
| `metadata.py` | shairport-sync 元数据管道解析器（XML + base64 + hex 代码兼容） |
| `ringbuffer.py` | 无锁读/有锁写的 PCM 环形缓冲，带 generation（代）语义 |
| `timeline.py` | `AudioTimeline`：AirPlay RTP/单调时钟 ↔ 曲目位置（第 13 节） |
| `ssdp.py` | SSDP M-SEARCH / 响应解析 / 多网卡收发 |
| `upnp.py` | 设备描述解析、SOAP 调用、GENA 订阅与 NOTIFY 接收 |
| `renderer.py` | Renderer 注册表、在线/离线判定、协议能力（protocolInfo）解析 |
| `stream.py` | 实时 HTTP PCM/WAV 流服务 + Range/HEAD |
| `state.py` | `PlaybackState` 统一状态机与 AirPlay↔DLNA 同步（第 10、11 节） |
| `webui.py` | REST API + 静态 UI 服务 |
| `supervisor.py` | nqptp / shairport-sync 进程监管与重启 |
| Web UI 前端 | 原生 HTML/CSS/JS |

---

## 4. AirPlay 音频如何进入 DLNA

### 4.1 数据通路（真实实现）

```text
iPhone/iPad/Mac
      │ AirPlay 2 (RTSP + RTP/RTCP, 加密)
      ▼
shairport-sync  ──(NQPTP 提供 PTP 时钟, 端口 319/320)
      │ 解密 + ALAC/AAC 解码 + 重采样/混音
      │ 输出：S16_LE / 44100 / 2ch 原始 PCM
      ├──────────────► FIFO  ${TRIM_PKGVAR}/audio.fifo   (pipe 后端)
      └──────────────► FIFO  ${TRIM_PKGVAR}/metadata.fifo (metadata 管道)
                                  │
                                  ▼
                       bridge.py (Python 原生进程)
                        ├─ 音频读取线程：FIFO → PcmRingBuffer
                        ├─ 元数据读取线程：解析事件 → PlaybackState
                        └─ HTTP 服务：GET /stream.wav → 环形缓冲实时流出
                                  │
                                  ▼
                    UPnP SetAVTransportURI(http://NAS:8788/stream.wav?gen=N)
                                  │
                                  ▼
                         DLNA / UPnP 音响（自行拉流播放）
```

**关键点**：桥接器**从不把整首曲子落盘**。PCM 从 FIFO 读出后进入内存环形缓冲，
由 DLNA 渲染器通过 HTTP **边产生边拉取**（`Content-Length` 未知或按已知总时长声明），
是真正的实时流，而不是“先存 WAV 再播放”。

### 4.2 为什么用 `pipe` 后端而不是 `stdout`

- `pipe` 后端写入**命名管道（FIFO）**，shairport-sync 与桥接器是**两个独立进程**，
  耦合度最低；桥接器崩溃不会连带杀死接收器，反之亦然（第 21 节生命周期要求）。
- `stdout` 后端要求把 shairport-sync 作为桥接器的子进程并共享管道，
  任何一方退出都会产生 SIGPIPE 连锁；且日志与音频共用 stdout 会互相污染。
- `pipe` 后端支持配置输出格式/采样率/声道，见第 7 节。

### 4.3 FIFO 与背压

`pipe` 后端在 `play()` 中**阻塞写** FIFO。桥接器的音频读取线程持续把 FIFO 排空进
环形缓冲，因此正常情况下 FIFO 不阻塞。只有当渲染器长时间停止消费、环形缓冲写满时，
读取线程才会暂停，从而让 FIFO 填满并**对 shairport-sync 形成背压**——这正是期望行为：
不能让内存无限增长。环形缓冲容量按“分钟”量级配置（默认 120 秒），
配合看门狗：若渲染器超过该窗口不消费，则主动重启 DLNA 会话而不是无限堆积。

---

## 5. PCM / WAV / FLAC / AAC 的处理方式

| 环节 | 处理 |
|---|---|
| AirPlay 侧输入 | ALAC（无损，Realtime/Buffered）或 AAC（有损）。由 shairport-sync 解密并解码 |
| 桥接器看到的格式 | 统一为 **S16_LE / 44100 Hz / 2ch**（在 shairport-sync 的 `pipe` 节固定） |
| 为什么固定 44.1k/S16 | DLNA `audio/L16` 与 `audio/wav` 的事实标准是 44.1k 或 48k、16 bit；S16 兼容性最好，且带宽低（176.4 KB/s）。shairport-sync 用 SoXR 高质量重采样与混音把任意输入（44.1k/48k、S16/S24/F24、2/5.1/7.1）统一到该格式 |
| 对外封装 | 首选 **WAV（`audio/wav`，RIFF/LPCM）**；若渲染器 protocolInfo 不支持 WAV，则退化为 **裸 LPCM（`audio/L16;rate=44100;channels=2`）** |
| FLAC | **不使用**。理由：① 需要引入编码器（libFLAC / ffmpeg 编码器），显著增大体积与 CPU；② 首阶段的目标设备是“没有 AirPlay 2 的 DLNA 音响”，其中支持 WAV/LPCM 的远多于支持 FLAC 的；③ 无损再压缩一遍对已经无损的 PCM 没有收益。`protocolInfo` 中若出现 FLAC 但无 WAV/L16，则视为**不支持**并在 UI 提示（第 13 节异常路径） |
| AAC | 作为**输入**解码支持（FFmpeg `aac` 解码器静态链接）。**不**作为输出格式：把 PCM 重新编码成 AAC 会引入有损二次编码与编码延迟，且 DLNA 的 AAC 流封装（ADTS vs LATM）在设备间极不统一 |

### WAV 实时流的头构造

对 WAV，为了让渲染器能显示**总时长**并支持进度，桥接器在知道时长时写出**带完整
`data` 块大小**的 RIFF 头：

```
RIFF <size> WAVE fmt  (16)  PCM(1) 2ch 44100 byterate=176400 blockalign=4 bits=16
data <duration_bytes>
```

- `duration_bytes = round(duration_sec * 176400)`，`duration_sec` 来自 AirPlay 的
  `prgr`（`(rtpstampend-rtpstampstart)/rate`）或 `astm` 元数据（毫秒）。
- 实际写入的 PCM 若短于声明长度：用**静音补齐**到声明长度（避免渲染器等待超时）。
- 实际写入若长于声明长度：截断并向渲染器发 `Stop`，随后按新时长重启流。
- 若时长未知：退化为 `data` 长度 `0xFFFFFFFF` + chunked（无 `Content-Length`），
  UI 中时长显示为 `--:--`。绝不为“未知时长”伪造一个随机值。

---

## 6. 如何实现实时流

1. **环形缓冲（`PcmRingBuffer`）**
   - 固定容量（默认 120 秒音频 = 21.2 MB）。
   - 单调递增的绝对字节偏移 `write_offset`；读者各自持有自己的 `read_offset`。
   - `generation`（代）计数器：任何“不连续”（seek / 换曲 / 会话重建）都会 `gen++`
     并清空缓冲。持有旧代的读者会收到 `StaleGeneration`，其 HTTP 连接被**主动关闭**，
     促使渲染器重新发起 GET，从而从新位置开始播放。
2. **HTTP 输出**
   - `GET /stream.wav?gen=N`、`GET /stream.pcm?gen=N`
   - 服务线程 `read(offset, max_bytes, timeout)` 阻塞等待数据；有新数据即写出并 flush。
   - 渲染器按 1x 消费 → 自然形成流控，无需自行节拍。
   - 支持 `HEAD`（渲染器探测用）与 `Range: bytes=0-`（部分设备先探测再全量拉取）。
3. **不落盘**：全程内存，无临时 WAV 文件。
4. **看门狗**：若某代在 `gen_idle_timeout`（默认 90 s）内无读者，或渲染器长时间
   0 进度而缓冲持续增长，则重建 DLNA 会话（`Stop` → `SetAVTransportURI` → `Play`）。

### 6.1 DLNA 渲染器互操作细节（依据 AirConnect 1.12.4 的实战做法）

| 项目 | 做法与理由 |
|---|---|
| 流 URL | 使用**唯一路径** `/stream/<token>.<ext>`（token 含时间戳与递增序号），而不是查询串。部分渲染器会按 URI 缓存并复用连接，唯一路径强制它重新 GET。AirConnect 用 `/stream-<n>.flac` 正是这个原因 |
| 时长未知时的 WAV 头 | 使用 AirConnect 经过实战验证的「超大长度」常量：`data = 0xFFFFFF00`、`riff = data + 36`。**不用** `0xFFFFFFFF`，个别解析器会拒收 |
| 时长已知时 | 写出**真实** `data` 长度 + `Content-Length`，实际数据不足时补静音，让渲染器能显示总时长与进度 |
| `Content-Length` 未知时 | 不发送 `Content-Length`，只发 `Connection: close`，以 EOF 表示流结束（对 HTTP/1.0 栈最兼容） |
| DLNA 响应头 | 恒发 `transferMode.dlna.org: Streaming`；当渲染器请求 `getcontentFeatures.dlna.org` 时回 `contentFeatures.dlna.org: <protocolInfo>`（含 `DLNA.ORG_OP=00` 表示不支持字节范围） |
| `Range` 请求 | `start == 0` 时按「从当前位置完整重发」返回 200（向渲染器声明不支持范围，AirConnect 的成熟做法）；`start > 0` 且数据仍在缓冲中时返回 206 + `Content-Range` |
| 媒体元数据 | `SetAVTransportURI` 带完整 **DIDL-Lite**（`dc:title`/`dc:creator`/`upnp:artist`/`upnp:album`/`upnp:albumArtURI`/`upnp:class`）。时长已知用 `object.item.audioItem.musicTrack` + `res@duration`；未知用 `object.item.audioItem.audioBroadcast`。这是渲染器显示标题与进度的关键 |
| 拉流前预滚动 | `SetAVTransportURI` 前等 `preroll_seconds`（默认 2 s）数据就绪，避免渲染器一开机就缓冲空洞 |
| `SetAVTransportURI` → `Play` | 两者之间留 0.3 s，部分渲染器需要准备时间；`Play` 失败会重试一次 |

---

## 7. 如何实现 Seek

AirPlay 的 seek 由**发送端**驱动：用户拖动进度条后，发送端会先发**flush**，
再从新位置继续发送音频。桥接器通过元数据管道的 **`pfls`**（play stream flush，
载荷为要 flush 到的帧号）感知到这一事件。

处理序列：

```text
收到 pfls (seek/flush)
  1. ringbuffer.flush()                 → generation++
  2. 记录 is_seeking = true，进入 BUFFERING
  3. DLNA: Stop(InstanceID=0)
  4. 等到新代积累到 preroll_seconds（默认 2 s）PCM
  5. DLNA: SetAVTransportURI(InstanceID=0,
             CurrentURI = http://NAS:{port}/stream.wav?gen={N},
             CurrentURIMetaData = DIDL-Lite(...))
  6. DLNA: Play(InstanceID=0, Speed=1)
  7. 以新的 AudioTimeline 锚点重建位置基准（第 13 节）
```

**为什么不用 `AVTransport#Seek` 做真正的拖动**：

本方案的 HTTP 资源是**实时生成的、位置不可寻址**的流——渲染器请求 URI 时，
我们只能从“当前 AirPlay 播放点”开始提供数据，无法提供 t=0 到 t=1:32 的历史数据
（那需要把整首缓存下来，正是任务书第 8 节禁止的做法）。

因此：
- **听感上**，seek 由“新代 + 重新 SetAVTransportURI + Play”实现，音频会跳到正确位置；
- **状态上**，桥接器维护的 `AudioTimeline` 立刻把 `position` 重锚到新位置，
  所以 Web UI 与状态机不会出现“时间跳错/继续增长”；
- **刻意不对实时流调用 `AVTransport#Seek`**。原因：本方案的 HTTP 资源是持续生成的
  实时流，渲染器侧的 `RelTime` 始终相对于「当前这一代流」的起点；对它做 Seek 只会让
  渲染器在自己那条流内乱跳，没有意义。正确做法就是「换 URI 重开一代」，这也正是
  AirConnect 在同一场景下采用的模型（其 `FLUSH → Stop → 新的唯一 URI + Play`）。
- `upnp.py` 仍然实现了符合 AVTransport:1 规范的
  `Seek(InstanceID=0, Unit=REL_TIME, Target=HH:MM:SS)`（例如 `00:01:32`），
  供未来「可寻址的有限时长资源」模式复用；当前实时流模式不调用它。
  （注意：AirConnect 1.12.4 的 `AVTSeek` 把 `Unit`/`Target` 两个参数写反了，
  且该函数在该版本中没有任何调用点；本项目按规范实现，不复制该缺陷。）

---

## 8. 如何实现 Pause / Resume

| AirPlay 事件 | 桥接器行为 |
|---|---|
| `paus`（buffered audio 暂停） | `PlaybackState.state = PAUSED`；冻结 `AudioTimeline`；**优先** `AVTransport#Pause`。若渲染器不支持暂停实时流（`Pause` 返回错误码 701/`TRANSITION_NOT_AVAILABLE`），退化为 `Stop` 并记住位置，恢复时重建会话 |
| `pres` / `pbeg`（恢复/开始播放） | 若渲染器处于 `PAUSED_PLAYBACK` → 直接 `AVTransport#Play`（保持同一 URI，位置连续）。若此前被迫 `Stop`，则走 seek 序列按记忆位置重建 |
| `pbeg`（新会话开始） | `ringbuffer.flush()`，重建 DLNA 会话（`Stop`→`SetAVTransportURI`→`Play`） |
| `pend`（播放结束/断开） | `PlaybackState.state = STOPPED`；`AVTransport#Stop`；清空 `title/artist/album/artwork`；**释放** buffer 代际与 HTTP 连接 |
| `pffr`（首个已计时帧） | 建立 `AudioTimeline` 锚点，用于精确位置推算 |
| `pdis`（时间戳不连续） | 视为隐式 flush，按 seek 处理 |

### 10.1 音量映射与回声抑制

`pvol` 事件载荷为 `"airplay_volume,volume,lowest,highest"`（dB）。shairport-sync 文档说明
`airplay_volume` 在 iOS 音量滑块上是**线性**的（0.00 → -30.00，`-144.00` 表示静音），
因此按滑块位置线性映射即可与用户所见一致：

```
percent = round((airplay_db + 30) / 30 * 100)     # 钳制到 0..100
mute    = (airplay_db <= -144)
```

渲染器回报的音量在 **1 秒窗口**内被忽略（回声抑制），避免「我们刚写入的值又被读回来」
造成来回抖动；窗口之外渲染器上报的变化（例如用户直接拧音响旋钮）则如实显示在 UI 上。
注意 AirPlay 接收端**无法反向设置发送端音量**，所以音量同步是
「iPhone → DLNA」单向，这符合任务书第 11 节的验收要求。

**关键设计**：桥接器**不**把 AirPlay 指令逐条直译成 SOAP（任务书第 10 节明确禁止）。
所有事件先归一到内部 `PlaybackState`，再由一个**状态同步器**做“目标状态 vs 渲染器
实际状态”的收敛（收敛循环 + 去抖 + 重试），因此：

- 短时间内 Play/Pause 抖动不会产生 SOAP 风暴；
- 渲染器暂时不可达时状态被记录为“待收敛”，设备恢复后自动补齐；
- 渲染器上报的状态（`GetTransportInfo` / GENA）与内部状态不一致时，以**渲染器实际状态**
  为 `rendererState` 字段对外暴露，避免 UI 说谎。

---

## 9. 如何同步播放时间（AirPlay 2 Timing ↔ DLNA Timing）

这是本项目最核心的工程点，任务书第 13 节要求“连续播放 30 分钟以上位置不持续漂移”。

### 9.1 三个时间基准

| 基准 | 来源 | 含义 |
|---|---|---|
| AirPlay RTP 时间戳 | `prgr`、`phbt`/`phb0` | 流内音频帧的编号（按采样率换算为秒） |
| AirPlay 单调时钟 | `phbt`/`phb0` 的第二个字段 | 该帧**应当被播放**的 `CLOCK_MONOTONIC_RAW` 纳秒时刻 |
| DLNA 渲染器时钟 | `GetPositionInfo.RelTime` / GENA `LastChange` | 渲染器自报的**当前流内**播放位置 |

### 9.2 `prgr` / `phbt` 的确切语义（源码核实）

- `prgr`（每秒一次，来自 RTSP `SET_PARAMETER progress:`）载荷为**纯文本**：
  `"<rtpstampstart>/<rtpstampnow>/<rtpstampend> <rate>"`
  → `position = (rtpstampnow - rtpstampstart)/rate`，
    `duration = (rtpstampend - rtpstampstart)/rate`。
- `phbt`（按 `metadata.progress_interval` 周期）与 `phb0`（会话首帧）载荷为**纯文本**：
  `"<rtp_timestamp>/<should_be_time_ns>"`，其中 `should_be_time_ns` 基于
  `CLOCK_MONOTONIC_RAW`。消息在帧进入输出缓冲时发出，因此**领先实际播放约
  `audio_backend_buffer_desired_length_in_seconds`**（默认 1.0 s）。

### 9.3 AudioTimeline 模型

锚点 `A = (rtp_ref, mono_ref_ns, track_ref_ms)`，其中 `track_ref_ms` 是该 RTP 参考点
对应的**曲目内位置**。于是任意单调时刻 `t`：

```
stream_pos_sec(t) = (rtp_ref - rtpstampstart)/rate + (t - mono_ref_ns)/1e9
track_pos_ms(t)   = track_ref_ms + stream_pos_sec(t) * 1000
```

- 收到 `prgr` 时重算 `track_ref_ms`（`prgr` 是权威的曲目位置/时长来源）。
- 收到 `phbt`/`phb0` 时更新 `(rtp_ref, mono_ref_ns)` 锚点。
- `PAUSED`/`STOPPED` 时冻结：位置取暂停瞬间的 `track_pos_ms`，不再随时间增长
  （任务书明确禁止“Pause 后时间继续增长”）。

### 9.4 与 DLNA 渲染器时钟的融合

渲染器只知道**它自己那条流**的位置（`RelTime`，从该代流的 0 开始）。桥接器维护
每代流的**位置偏移** `gen_offset_ms`，于是：

```
对外 position_ms = gen_offset_ms + renderer_rel_time_ms + renderer_output_latency_ms
```

- `gen_offset_ms` 在每次新建代（`pbeg`/`pfls`）时由 `AudioTimeline` 给出（即
  “这一代流的第 0 字节对应曲目的第几毫秒”）。
- `renderer_output_latency_ms` 是可配置常量（默认 0），用于补偿渲染器自身的
  输出缓冲；用户可在 UI 微调（`av_offset_ms`）。
- **漂移控制**：以 `AudioTimeline` 为“真值”，以渲染器 `RelTime` 为“观测值”。
  当两者偏差超过阈值（默认 1500 ms）时判定渲染器落后/超前，触发重建会话对齐；
  偏差在阈值内时**只做显示**，不打扰播放（避免频繁 SOAP）。
- **长时播放**：因为位置由单调时钟 + RTP 锚点推算，而渲染器时钟仅用于**观测**，
  所以 30 分钟级别的累积漂移不会污染内部状态；同时监控
  `Δ = timeline_pos - (gen_offset + rel_time)` 的**趋势**，持续单向漂移即判定
  渲染器采样时钟不准，触发一次重建（重建后偏移归零）。

### 9.5 GENA 抖动

GENA `LastChange` 推送是**事件驱动**的，可能密集。同步器对位置类事件做
**最小更新间隔**限流（默认 1 s），与轮询策略一致，避免“每几十毫秒一次 SOAP”。

---

## 10. 如何实现 DLNA Eventing

1. **订阅**：启动 HTTP 回调服务器（复用 `:8788`，路径 `/upnp/notify/<token>`），
   `SUBSCRIBE` 到渲染器 `eventSubURL`：
   ```
   SUBSCRIBE <eventSubURL> HTTP/1.1
   HOST: <ip:port>
   CALLBACK: <http://<nas-ip>:8788/upnp/notify/<token>>
   NT: upnp:event
   TIMEOUT: Second-1800
   ```
   → 响应 `SID: uuid:...`，按 `TIMEOUT` 的 80% 周期**续订**。
2. **接收 NOTIFY**：渲染器 `POST` `LastChange`（`Event` 属性，XML），解析
   `AVTransport` / `RenderingControl` 变量（`TransportState`、`CurrentTrackDuration`、
   `RelativeTimePosition`、`Volume`、`Mute`）。
3. **回退**：若 `SUBSCRIBE` 失败或不支持 GENA，则轮询
   `GetPositionInfo` + `GetTransportInfo`，**基准间隔 3 s**，并加入 ±20% 抖动，
   且仅在播放中轮询；`STOPPED` 时降为 15 s。
4. **绝不**每几十毫秒调用 SOAP。

---

## 11. 如何处理 Renderer 不支持某种格式

启动或选中 Renderer 时调用 `ConnectionManager#GetProtocolInfo`，解析 `Sink`
（逗号分隔的 `protocolInfo` 三元组 `protocol:network:mime:additional`）。

选择顺序（任务书第 9 节建议顺序）：

```
1. http-get:*:audio/wav:*            （含 audio/x-wav, audio/wave）
2. http-get:*:audio/L16;*            （裸 LPCM；含 rate/channels 参数匹配）
3. http-get:*:audio/x-wav:*
4. 其它 PCM 类（audio/L8 / audio/L24 / audio/basic）
```

- 若命中 1/3 → `Content-Type: audio/wav`，RIFF 封装。
- 若仅命中 2 → `Content-Type: audio/L16;rate=44100;channels=2`，裸流。
- 若**都不命中** → 记 `ERROR`，日志明确写出
  `Renderer does not support required MIME type`，并在 UI 上把该设备标为
  “不支持实时 PCM 流”，**不静默切换 MIME**、不猜测。
- 无法获取 `protocolInfo` 时（部分设备 `GetProtocolInfo` 超时）：保守地按
  `audio/wav` 尝试一次；失败后向用户回报明确的 MIME 拒绝原因。
- 若渲染器支持 FLAC 但不支持 WAV/L16：明确判定为不支持（第 5 节理由），
  日志与 UI 均给出可操作提示。

---

## 12. mDNS 如何注册

**由 shairport-sync 通过系统 Avahi（D-Bus）完成**（构建时 `--with-avahi`）。

- 注册 **两个** 服务类型：
  - **`_airplay._tcp`** —— AirPlay 2 的服务记录，携带 HomeKit 配对公钥与能力位。
  - `_raop._tcp` —— Classic AirPlay（RAOP）记录，用于向后兼容。
- TXT 记录由 shairport-sync 生成，实测在线抓取结果（`avahi-browse -rt _airplay._tcp`）：
  `fv`、`vv=2`、`osvers`、`srcvers`、**`pk=<HomeKit 配对公钥>`**、`psi`、`pi`、`gid`、
  `protovers=1.1`、`model`、`flags`、**`features=0x405FCA00,0x18340`**、`fex`、`deviceid`、`acl`。
  其中 `pk`/`features`/`protovers` 是判断「真正的 AirPlay 2 设备」的关键字段。
- **服务名 = AirPlay 音箱名称**（`general.name`）。桥接器在启动 shairport-sync **之前**
  把该名称写入其配置文件，因此改名需要**重启 shairport-sync 子进程**（不需要重启整个
  应用）；改名完全不影响 DLNA 选择，因为两者是彼此独立的字段（任务书第 5 节）。

### 为什么最终选择 Avahi 而不是内置 tinysvcmdns

最初为规避依赖，本项目曾构建 `--with-tinysvcmdns`。**实测证明这是错误的**，已修正：

1. **tinysvcmdns 只注册 `_raop._tcp`。** 源码核实：`config.regtype2`
   （默认 `_airplay._tcp`）**仅在 `mdns_avahi.c` 中被使用**
   （`mdns_avahi.c:277`、`:399`），而 `mdns_tinysvcmdns.c` 只注册 `config.regtype`
   （默认 `_raop._tcp`）。用 tinysvcmdns 构建时，实测 `avahi-browse _airplay._tcp`
   **完全为空**，Apple 设备只能把本机识别为 AirPlay 1（RAOP）设备——这直接违反
   任务书第 4 节「不要退化为 AirPlay 1 / RAOP」。
2. 改用 `--with-avahi` 后，实测 `_airplay._tcp` 正常出现，TXT 记录完整（见上）。
3. **开发包冲突的解决办法**：飞牛 OS 自带 `2:0.8-10+deb12u1+trim-1` 版本的 Avahi，
   与 Debian `libavahi-client-dev` 的精确版本依赖冲突，无法 `apt install`。
   解决办法是 **只下载并解包**（`apt-get download` + `dpkg-deb -x`）到构建用临时目录，
   用 `CFLAGS`/`LDFLAGS`/`PKG_CONFIG_PATH` 指向它进行编译，**不安装、不替换系统包**。
   运行期链接的是飞牛自带的 `libavahi-client.so.3` / `libavahi-common.so.3`
   （同上游版本，ABI 一致），因此不需要随包分发 Avahi。
4. 运行期依赖系统 `avahi-daemon` + D-Bus。飞牛 OS 默认运行二者（实测本机
   `avahi-daemon` 常驻），且调试确认 **应用用户** 具备通过 D-Bus 发布服务的权限
   （以 `air2dlna` 用户执行 `avahi-publish` 成功并可被浏览到）。
5. 不把服务绑定到 loopback：mDNS 在 LAN 接口上组播（第 17 节）。

> 构建脚本同时保留 `--with-tinysvcmdns` 分支作为**无 Avahi 环境的降级选项**，
> 但会明确提示该模式下 AirPlay 2 的 `_airplay._tcp` 记录不可用。

## 13. SSDP 如何发现

1. **接口选择**：用 `netif.py` 选出候选 LAN 接口（第 17 节）。
2. **M-SEARCH**（每个候选接口的每个 IPv4/IPv6 地址各发一轮）：
   ```
   M-SEARCH * HTTP/1.1
   HOST: 239.255.255.250:1900
   MAN: "ssdp:discover"
   MX: 2
   ST: urn:schemas-upnp-org:device:MediaRenderer:1
   ```
   随后追加一轮更宽的 `ST: ssdp:all` 作为兜底（部分设备只用 `upnp:rootdevice` 回应）。
   UDP 源端口随机（不使用固定端口，避免与系统 SSDP 冲突），设置
   `IP_MULTICAST_TTL=2`、`IP_MULTICAST_IF` 指定出口接口。
3. **收包**：窗口 `MX` 秒（默认 3 s/轮，共 3 轮，轮间 1.5 s），解析
   `LOCATION` / `USN` / `ST` / `SERVER` 头。
4. **去重**：以 `UDN`（USN 去掉 `::urn:...` 后缀）为主键。
5. **设备描述**：GET `LOCATION`（超时 5 s，重试 2 次），解析 XML：
   `friendlyName`、`UDN`、`manufacturer`、`modelName`、`deviceType`，
   以及 `serviceList` 中的 `AVTransport` / `RenderingControl` / `ConnectionManager`
   的 `controlURL` / `eventSubURL`（相对 URL 按 `LOCATION` 解析为绝对 URL）。
6. **只保留** `deviceType` 含 `MediaRenderer` **且**具备 `AVTransport` 服务的设备。
7. **在线判定与重发现**：
   - 周期后台重扫（默认 5 分钟）更新在线集合；
   - 已选 Renderer 若从 SSDP 消失或 SOAP 连续失败 → 标记 `rendererState = offline`，
     UI 显示**离线**；设备重新出现后**自动**恢复同一 `UDN` 的在线状态；
   - **绝不自动切换到另一台设备**（任务书第 7 节）：`selected_renderer_udn` 是唯一依据，
     离线时只提示，由用户手动重选。

---

## 14. 飞牛 FPK 目录结构

```text
airplay2-dlna-bridge/                # 源码仓库根
├── TECHNICAL_DESIGN.md              # 本文档
├── README.md
├── manifest                         # fnOS 包清单（键值，无扩展名）
├── ICON.PNG                         # 64x64
├── ICON_256.PNG                     # 256x256
├── config/
│   ├── privilege                    # run-as / 用户名 / 组名
│   └── resource                     # 资源与 API scope（本项目无需额外 scope）
├── cmd/                             # 生命周期脚本
│   ├── main                         # start | stop | status
│   ├── install_init
│   ├── install_callback
│   ├── upgrade_init
│   ├── upgrade_callback
│   ├── config_init
│   ├── config_callback
│   ├── uninstall_init
│   └── uninstall_callback
├── wizard/
│   └── install                      # 安装向导（端口 / AirPlay 名称）
└── app/
    ├── ui/                          # Web UI（desktop_uidir=ui）
    │   ├── config                   # 桌面入口定义
    │   ├── index.html
    │   ├── css/app.css
    │   ├── js/app.js
    │   └── images/icon_64.png, icon_256.png
    └── server/
        ├── bridge.py                # 主进程入口
        ├── air2dlna/            # Python 包（仅标准库）
        │   ├── config.py  logging_setup.py  netif.py
        │   ├── ringbuffer.py  timeline.py  metadata.py
        │   ├── ssdp.py  upnp.py  renderer.py
        │   ├── stream.py  state.py  webui.py  supervisor.py
        │   └── __init__.py
        ├── bin/
        │   ├── shairport-sync       # 自编译，AirPlay 2
        │   └── nqptp                # 自编译，PTP 时钟
        └── lib/                     # 随包 .so（RPATH=$ORIGIN/../lib）
            ├── libconfig.so.9  libsoxr.so.0  libplist-2.0.so.3
            ├── libpopt.so.0  libsodium.so.23  libgcrypt.so.20
            └── (libcrypto/libgomp/libuuid/libgpg-error 使用系统自带)
```

安装后（fnOS 规范）：

```text
/var/apps/air2dlna/
├── manifest  cmd/  config/  wizard/  ICON*.PNG
├── target  -> /vol{n}/@appcenter/air2dlna   （= TRIM_APPDEST）
├── etc     -> /vol{n}/@appconf/air2dlna     （= TRIM_PKGETC，配置）
├── var     -> /vol{n}/@appdata/air2dlna     （= TRIM_PKGVAR，运行数据/日志/FIFO）
├── tmp     -> /vol{n}/@apptemp/air2dlna     （= TRIM_PKGTMP）
└── home    -> /vol{n}/@apphome/air2dlna     （= TRIM_PKGHOME）
```

**不硬编码安装卷与路径**：全部通过 `TRIM_APPDEST` / `TRIM_PKGETC` / `TRIM_PKGVAR` /
`TRIM_PKGTMP` / `TRIM_PKGHOME` / `TRIM_SERVICE_PORT` 取得。

---

## 15. 后台服务生命周期

`cmd/main` 支持 `start` / `stop` / `status`（`status` 运行返回 0，未运行返回 3，
未知参数返回 1）。

**start（幂等）**

1. 校验运行环境：`python3.12`（来自 `python312` 依赖应用）、`bin/shairport-sync`、
   `bin/nqptp`、端口可用性；缺失时把**用户可读的中文原因**写入 `${TRIM_TEMP_LOGFILE}`
   并非零退出。
2. 准备运行目录：`${TRIM_PKGVAR}`、FIFO（`audio.fifo`、`metadata.fifo`，先删后建）。
3. 生成 shairport-sync 配置（名称/端口/格式/管道路径均来自应用配置）。
4. 启动 **nqptp**（以 root，端口 319/320，`CAP_NET_BIND_SERVICE` 所需）。
5. 启动 **shairport-sync**（降权到应用用户）。
6. 启动 **bridge.py**（降权到应用用户；HTTP 端口 `${TRIM_SERVICE_PORT}`）。
7. 写 PID 文件并做启动健康检查（进程存活 + HTTP `/api/health` 可应答）；
   失败则回滚（停掉已启动的子进程）并写 `TRIM_TEMP_LOGFILE`。

**stop（幂等）**：先 `TERM` bridge.py（优雅：停止 DLNA 会话、关闭 FIFO、关闭 HTTP），
等待至多 10 s，必要时 `KILL`；随后停 shairport-sync、nqptp；清理 PID 与 FIFO。

**status**：以 PID 文件 + 进程存活双重校验；陈旧 PID 文件自动清理并返回 3。

**upgrade**：`upgrade_init` 幂等停服；`upgrade_callback` 执行**幂等配置迁移**
（补默认字段、`schema_version` 升级），**不删除**用户配置与日志。

**uninstall**：`uninstall_init` 停服；`uninstall_callback` 按 fnOS 规范处理
`@appconf`（配置）与 `@appdata`（运行数据）的保留/删除，不触碰应用目录之外的数据。

**开机自启**：由 fnOS 应用中心按 `manifest` 的 `ctl_stop` 与安装卷策略管理，
应用侧不额外写 systemd 单元（避免与飞牛的进程管理重复）。

---

## 16. 配置文件位置

| 内容 | 路径 | 说明 |
|---|---|---|
| 应用配置（持久） | `${TRIM_PKGETC}/config.json` | 结构化 JSON，含 `schema_version` |
| 日志 | `${TRIM_PKGVAR}/bridge.log`（按大小轮转） | 桥接器日志 |
| shairport-sync 日志 | `${TRIM_PKGVAR}/shairport-sync.log` | 接收器日志 |
| nqptp 日志 | `${TRIM_PKGVAR}/nqptp.log` | 时钟守护进程日志 |
| 生成的 shairport-sync 配置 | `${TRIM_PKGVAR}/shairport-sync.conf` | 由桥接器按应用配置生成 |
| PCM FIFO | `${TRIM_PKGVAR}/audio.fifo` | 音频 |
| 元数据 FIFO | `${TRIM_PKGVAR}/metadata.fifo` | 控制/元数据 |
| PID | `${TRIM_PKGVAR}/*.pid` | 生命周期 |

配置字段（任务书第 19 节要求 + 扩展）：

```json
{
  "schema_version": 1,
  "airplay_name": "Feiniu AirPlay",
  "selected_renderer_udn": "",
  "selected_renderer_name": "",
  "selected_renderer_ip": "",
  "log_level": "info",
  "rtsp_port": 7000,
  "http_port": 8788,
  "output_format": "wav",
  "preroll_seconds": 2.0,
  "buffer_seconds": 120,
  "av_offset_ms": 0,
  "drift_threshold_ms": 1500,
  "metadata_poll_seconds": 3.0,
  "rediscover_seconds": 300
}
```

**升级不删用户配置**：迁移只新增/规范化字段，保留 `airplay_name` 与
`selected_renderer_*`。

---

## 17. 日志位置与内容

位置：`${TRIM_PKGVAR}/bridge.log`（主），`shairport-sync.log`、`nqptp.log`。
日志级别 `log_level` 可调（`debug`/`info`/`warn`/`error`）。按大小轮转（默认 5 MB × 5）。

按任务书第 20 节要求，日志覆盖并包含**具体失败原因**，例如：

```
AirPlay service started (name="客厅音响", rtsp_port=7000)
mDNS service registered (_airplay._tcp)
DLNA discovery started (interfaces=["eth0/192.168.1.50"])
Renderer found udn=uuid:... name="Living Room Speaker" ip=192.168.1.x model=...
Renderer selected udn=uuid:... name="Living Room Speaker"
AirPlay client connected (device="iPhone", ip=192.168.1.y)
AirPlay session established
Audio format: ALAC 44100Hz 16bit 2ch
SetAVTransportURI uri=http://192.168.1.50:8788/stream.wav?gen=3
Play / Pause / Seek(00:01:32) / SetVolume(42)
Renderer disconnected (udn=uuid:...) -> offline
AirPlay session ended
```

错误原因必须具体：

```
Renderer does not support required MIME type (sink=..., tried=audio/wav, audio/L16)
SetAVTransportURI rejected (UPnPError 714 Illegal MIME-Type)
AirPlay authentication failed
Audio stream timeout (no PCM for 5.0s)
DLNA SOAP timeout (action=Play, url=..., 5s)
Renderer unavailable (udn=uuid:...)
```

---

## 18. 安全性与权限

### 18.1 运行身份

- `config/privilege` 声明 `run-as: root`，**原因是 NQPTP 必须独占 UDP 319/320**
  （特权端口），而飞牛原生应用没有 systemd 的 `AmbientCapabilities` 机制可用；
  这是**功能必需**，不是图方便。
- **最小化 root 暴露面**：
  - 只有 **nqptp**（45 KB、单一职责、只收发 PTP 报文、不监听 TCP、不解析外部输入）
    以 root 运行；
  - **shairport-sync** 与 **bridge.py** 由 `cmd/main` 通过 `setpriv`/`runuser`
    **降权到应用用户** `air2dlna` 运行；降权失败时记录明确告警并按 root 继续
    （保证可用性优先，且日志可审计）。
- 不以 root 运行任何面向用户的 HTTP 服务（bridge.py 为应用用户）。

### 18.2 网络暴露面

| 端口 | 用途 | 绑定 | 鉴权 |
|---|---|---|---|
| `${TRIM_SERVICE_PORT}`（默认 8788） | Web UI / REST API / DLNA PCM 流 / GENA 回调 | `0.0.0.0`（LAN 必需） | UI/API 依赖 fnOS 桌面入口的登录态与内网边界；**写操作**（改配置/选择设备）额外要求 `X-Requested-With` 头，防 CSRF |
| 7000（可配） | AirPlay RTSP | `0.0.0.0` | AirPlay 2 配对由 shairport-sync 负责 |
| 319/320（UDP） | PTP（nqptp） | `0.0.0.0` | 仅 PTP 协议 |
| 动态高位端口 | RTP/RTCP/timing/事件回调 | LAN | 协议内部 |

- REST API **仅**暴露必要字段；**不**回显 `TRIM_API_TOKEN`（本项目不申请任何
  `api-scope`，也不需要访问用户文件）。
- `config/resource` 不含 `data-share`、不含 `api-scope`，即**不访问任何用户文件**，
  遵循最小权限。
- 输入校验：`airplay_name` 长度/字符白名单（避免注入到 shairport-sync 配置文件），
  `rtsp_port`/`http_port` 范围校验，`selected_renderer_udn` 必须是已发现集合中的值。
- 不把任何请求参数拼进 shell；启动子进程使用参数数组，不使用 `shell=True`。
- **不使用 `pkill -f <二进制路径>`**。该写法会匹配任何命令行中含有该路径的进程
  （实测会误杀运维人员的 shell / 日志跟踪命令），已在开发过程中造成真实误杀。
  所有进程终止一律基于 PID 文件（`app.pid`、`nqptp.pid`、`shairport-sync.pid`）精确执行。
- 生成的 shairport-sync 配置对字符串做转义（`"`、`\`、换行）后写入。

### 18.3 升级与数据

- 升级只增改配置字段，不删除用户数据；uninstall 按 fnOS 规范处理。
- 不写应用目录之外的文件（除 nqptp 的 `/dev/shm/nqptp`，这是 nqptp 的既定行为）。

---

## 19. 异常恢复设计（任务书第 15、23 节）

| 场景 | 行为 |
|---|---|
| iPhone 断开 | shairport-sync 发 `pend`/`disc` → 释放 session、清 buffer、清元数据、停 DLNA 会话、释放 HTTP 连接 |
| iPhone 重连 | 新 `conn`/`pbeg` → 重建完整 session 与 DLNA 会话，**不复用**旧代缓冲 |
| DLNA 音响断电 | SOAP 连续失败 → `rendererState=offline`，UI 显示“离线”；应用**不崩溃**（所有网络调用有超时与异常捕获）；设备重新上线后 SSDP 重新发现并自动恢复在线 |
| shairport-sync 崩溃 | `supervisor` 检测退出并重启，退避重试（1s→2s→5s→10s，上限 30s），日志写明退出码 |
| nqptp 崩溃 | 同上；nqptp 不可用时 shairport-sync 的 AirPlay 2 会失败，日志明确提示端口 319/320 |
| FIFO 读端消失 | shairport-sync 的 pipe 后端遇到 `EPIPE` 会重试打开，不崩溃 |
| HTTP 客户端中断 | 连接线程优雅结束，不影响其他客户端 |
| 长时间无 PCM | 看门狗记录 `Audio stream timeout`，并把状态置为 `BUFFERING`/`ERROR` |

---

## 20. 验收与测试计划

### 自动化（本仓库内可执行，不依赖真实 iPhone）

- `tests/fake_renderer.py`：在本机模拟一个 UPnP MediaRenderer
  （响应 SSDP、提供 device description、实现 AVTransport/RenderingControl/ConnectionManager
  SOAP、支持 GENA、能 GET PCM 流并校验数据完整性与时长），用于端到端验证
  发现→选择→SetAVTransportURI→Play→Pause→Seek→音量→位置同步。
- `tests/fake_airplay.py`：向 `audio.fifo` 写入合成 PCM（正弦/静音序列）、
  向 `metadata.fifo` 写入符合真实线格式的 XML 事件序列，用于验证
  缓冲、时间线、seek/pause 状态机、WAV 头与元数据解析。
- `tests/test_*.py`：`unittest` 单元测试（元数据解析、环形缓冲、时间线、SOAP 构造、
  protocolInfo 选择、配置迁移幂等）。
- `python scripts/validate_fnos_project.py <project>` 预检。

### 真机（任务书第 25 节 Test 1–12）

iPhone + 飞牛 NAS + DLNA 音响同一 LAN。逐项验证：AirPlay 发现（名称正确）、连接、
播放出声、暂停、恢复、Seek、上一首/下一首、音量、元数据/封面、30 分钟长播
（断流/卡顿/漂移/内存/CPU/状态一致性）、Renderer 断电与重发现、AirPlay 重连。

> 说明：本次交付在本机完成构建、单元/集成测试与 fnOS 安装/启动/Web UI 验证；
> 依赖真实 iPhone 与真实 DLNA 音响的 Test 1–12 需在用户的实际 LAN 环境执行，
> 仓库提供 `docs/ACCEPTANCE.md` 逐步操作清单。

---

## 21. 开发顺序（任务书第 26 节，映射到交付物）

| Phase | 内容 | 交付物 |
|---|---|---|
| 1 | AirPlay 2 Receiver | shairport-sync + nqptp 编译并冒烟验证 |
| 2 | DLNA Discovery | `netif.py` + `ssdp.py` + `upnp.py` 设备描述 |
| 3 | 最小音频链路 | `ringbuffer.py` + `stream.py` + `SetAVTransportURI/Play` |
| 4 | Play/Pause/Stop | `state.py` 状态机 + 同步器 |
| 5 | Seek/Volume | `timeline.py` 重锚 + `RenderingControl` |
| 6 | Metadata/Artwork | `metadata.py` + UI 展示 |
| 7 | Timing 同步 | `phbt/prgr` 锚点 + GENA/轮询 |
| 8 | 异常恢复 | `supervisor.py` + 离线/重连处理 |
| 9 | Web UI | 第 16 节界面 |
| 10 | FPK 打包 | `scripts/build.sh` + `fnpack build` |
| 11 | 真机测试 | `docs/ACCEPTANCE.md` |
