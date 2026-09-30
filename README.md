<p align="center"><img src="app/ui/images/icon_256.png" width="128" alt="Air2DLNA"></p>

<h1 align="center">Air2DLNA</h1>

<p align="center">把局域网里的 <b>DLNA / UPnP 音响</b>变成 <b>AirPlay 2 音箱</b> —— 飞牛 fnOS 原生应用，不需要 Docker。</p>

AirPlay 2 是 Apple 设备独占的协议，而大量音响只支持 DLNA/UPnP。Air2DLNA 在飞牛 NAS 上内置一个**真正的 AirPlay 2 接收器**（shairport-sync + NQPTP），把 Apple 设备推来的音频实时解码成 PCM，再通过标准 UPnP AVTransport 控制 + 实时 HTTP 流交给局域网 DLNA 音响。iPhone / iPad / Mac 的 AirPlay 列表里会直接多出一个音箱，使用体验与原生 AirPlay 2 音箱一致。

**核心特点**

- **真正的 AirPlay 2 接收**：注册 `_airplay._tcp`（含 HomeKit 配对公钥）与兼容用 `_raop._tcp`，不是 AirPlay 1 兼容层
- **实时桥接、不落盘**：PCM 经内存环形缓冲由渲染器边产生边拉取，全程不写磁盘
- **自动发现 + 能力协商**：SSDP 发现局域网 DLNA 设备，先解析 `protocolInfo` 再推流，设备不支持时明确说明原因
- **状态完整同步**：播放状态、标题/艺术家/专辑/封面、进度、音量；进程崩溃自动重启，渲染器掉线标记离线且不擅自切换设备
- **原生 fpk 应用**：应用中心一键安装/升级/卸载，内置 Web UI 管理 AirPlay 名称、设备选择与日志

```text
iPhone / iPad / Mac
        │  AirPlay 2（播放状态权威）
        ▼
┌──────────────────────────────────────────────┐
│ 飞牛 NAS 原生应用（无 Docker）                │
│  shairport-sync + NQPTP                       │
│        ↓ 解密 / ALAC·AAC 解码                  │
│  bridge.py：PCM 环形缓冲 + AirPlay 时间线      │
│        ↓                                      │
│  Virtual Player（唯一状态协调中心）            │
│   曲目 / 位置 / 时长 / 缓冲 / 状态机           │
│        ↓ 意图：PLAY / PAUSE / SEEK / TRACK     │
│  DLNA Output（AVTransport + 连续 HTTP 输出）   │
│   · 真实 PCM 不足时在输出层生成静音，绝不断流  │
│   · Renderer Profile（generic / xiaomi_s12）   │
└──────────────┬───────────────────────────────┘
               │ LAN（DLNA / UPnP）
               ▼
        DLNA / UPnP 音响
```

「Virtual Player」是 1.0.23 起的核心概念：AirPlay 决定**实际播放状态**，Virtual
Player 把这场 AirPlay 会话虚拟成一个正常的 DLNA 播放器，DLNA Output 再把它呈现给
音响。详见第 3 节。

---

## 1. 安装

```bash
# 在飞牛 NAS 上（应用中心也可直接手动安装 .fpk）
appcenter-cli install-fpk Air2DLNA-1.0.4.fpk --volume 1
appcenter-cli start air2dlna
```

依赖：应用中心里的 **Python 3.12**（`python312`，已在 `manifest` 中声明为
`install_dep_apps`，安装时自动准备）。**不需要 Docker**，不需要联网安装 pip 包。

### 首次使用

1. 打开应用：**应用中心 / 桌面点击本应用图标**（走飞牛统一网关）；也可直接在浏览器
   访问 `http://<NAS_IP>:8788`。
2. 在「AirPlay 音箱名称」里填一个名字（默认 `Feiniu AirPlay`），保存。
   这个名字会出现在 iPhone 的 AirPlay 列表里。
3. 点「搜索设备」，在「DLNA 播放设备」里选择你的音响。
4. 在 iPhone 控制中心 → AirPlay → 选择上面那个名字，即可播放。

之后日常使用完全不需要再打开 NAS。

> 桌面入口实现：应用中心/桌面图标通过飞牛统一网关访问应用（`gatewaySocket` +
> `gatewayPrefix`，见 `app/ui/config`），服务端监听 `target/air2dlna.sock` 并剥离
> `/app/air2dlna` 前缀。注意飞牛 `ui/config` 只认 `${...}` 形式的占位符，
> `{port}` / `{display_name}` 这类写法不会被替换（会导致入口打开后是空白页）。

---

## 2. 功能

| 能力 | 说明 |
|---|---|
| 真正的 AirPlay 2 接收 | 基于 shairport-sync 5.5.1（`--with-airplay-2`）+ NQPTP 1.2.8；注册 `_airplay._tcp`（含 HomeKit 配对公钥 `pk` 与 `features` 能力位）与兼容用的 `_raop._tcp`；mDNS 设备类别按**音频接收设备**广播，iOS 设备列表显示为音响/扬声器 |
| 音频格式 | 输入支持 ALAC 与 AAC（内置解码 + 静态链接的 FFmpeg AAC 解码器）；5.1/7.1 自动混音为立体声；44.1k/48k 自动重采样 |
| 实时桥接 | **不落盘**：PCM 经内存环形缓冲由 DLNA 渲染器通过 HTTP 边产生边拉取 |
| DLNA 发现 | SSDP + 设备描述解析（friendlyName / UDN / model / manufacturer / 服务端点） |
| 能力协商 | `ConnectionManager#GetProtocolInfo` 解析 `protocolInfo`，优先 `audio/wav`，回退 `audio/L16`；不支持时明确拒绝并给出原因 |
| 播放控制 | Play / Pause / Stop / Seek（重锚）/ 音量；统一内部 `PlaybackState`，不逐条直译 SOAP。1.0.23 起由 Virtual Player 统一协调：实际状态（`IDLE/BUFFERING/PLAYING/PAUSED/SEEKING/TRACK_SWITCHING/RECOVERING/STOPPING`）与请求状态（`*_REQUESTED`）严格分离 |
| 持续输出 | **DLNA 处于播放时，HTTP 音频输出必须连续**：真实 AirPlay PCM 暂时不足（恢复 / Seek / 切歌空窗）时在输出层生成静音填充，绝不主动 EOF；静音**只**存在于 DLNA Output 层，绝不写入 AirPlay RingBuffer / Timeline |
| 设备差异 | Renderer Profile：`generic`（保守：先尝试真正的 `AVTransport#Pause` 并保留 URI）与 `xiaomi_s12`（实测：Pause 后自行 STOPPED、不保持 HTTP、恢复必须重新宣告）。差异写在 Profile 里，不写死在 Virtual Player 核心 |
| 进度同步 | 使用 shairport-sync 的 `prgr` / `phbt` 时间戳 + 单调时钟建立 `AudioTimeline`；GENA 事件优先，轮询兜底 |
| 状态展示 | 播放状态（含真实状态机与请求状态）、标题/艺术家/专辑/封面、当前位置与总时长、音量、日志 |
| 播放稳定性 | **AirPlay 时间线是唯一权威时间线**：DLNA 的 `RelTime`、`GetPositionInfo`、内部缓冲延迟、HTTP `Range`、HTTP 重连都只是观测信息，绝不据此更换媒体生命周期。固定的位置偏差是渲染器缓冲延迟，不再触发换代重建（那会让音箱重新淡入 → 周期性声音变小又变大）。只有三种原因允许换代：真实 seek（`pfls`/`pdis`）、渲染器链路真的断了、播放真的结束 |
| 异常恢复 | 渲染器掉线标记离线（不自动切换设备）、进程崩溃自动重启；`pend` 视为**播放流结束**并进入短过渡态（不立即 Stop/不 flush/不清 session），窗口内没有新流才真正结束 → 拖动进度条后不再长时间无声；渲染器声称在播放却长时间无人拉流时兜底重建。恢复（new generation + SetURI + Play）只是 `RECOVERING` 状态下的 fallback，不是正常 Resume / Seek / Pause 路径 |

---

## 3. 架构（1.0.23 起：Virtual Player）

> 完整规格见 [`docs/ARCHITECTURE_V2.md`](docs/ARCHITECTURE_V2.md)，
> 变更记录见 [`CHANGELOG.md`](CHANGELOG.md)。

核心原则：

* **AirPlay 是播放状态权威（Playback Authority）**：Virtual Player 不自行创造
  「真实播放状态」；
* **Virtual Player 是唯一状态协调中心（State Coordinator）**：维护曲目/位置/时长/
  缓冲/时间线/会话与状态机；
* **DLNA Output 是输出渲染器（Output Renderer）**：把意图翻译成
  `SetAVTransportURI` / `Play` / `Pause` / `Stop` 与连续 HTTP 媒体输出。

### 3.1 模块

| 模块 | 职责 |
|---|---|
| `air2dlna/virtual_player.py` | VirtualPlayer：实际状态机 + 独立请求状态 + 曲目/时间线/暂停恢复/keepalive + 反向控制编排 |
| `air2dlna/dlna_output.py` | DLNAOutput：AVTransport 动作、媒体会话/URI/DIDL、连续 HTTP 输出（静音生成）、恢复原语、GENA 订阅 |
| `air2dlna/airplay_remote.py` | AirPlayRemoteController：DACP 客户端 + 逐项能力检测（SUPPORTED / UNSUPPORTED / UNKNOWN） |
| `air2dlna/renderer_profile.py` | RendererProfile：`generic` / `xiaomi_s12` 设备差异与缓冲水位 |
| `air2dlna/stream.py` | Virtual Media Output：实时 WAV/LPCM 流；`continuous_output` 时真实 PCM 不足则送静音、绝不断流 |
| `air2dlna/state.py` | `BridgeController`：组合根（组装上述模块 + 对外查询 + 历史接口转发） |
| `air2dlna/timeline.py` / `ringbuffer.py` | **保持不变**：AirPlay 权威时间线与真实 PCM 环形缓冲 |

### 3.2 状态机

实际状态与「控制请求」是两个维度，绝不混为一谈：

```text
实际状态：IDLE → BUFFERING → PLAYING ⇄ PAUSED
                      ↓           ↓
                   SEEKING   TRACK_SWITCHING
                      ↓           ↓
                  RECOVERING（仅异常链路）  STOPPING → IDLE

请求状态（独立）：PLAY_REQUESTED / PAUSE_REQUESTED / SEEK_REQUESTED /
                  NEXT_REQUESTED / PREVIOUS_REQUESTED
```

每个控制请求都带 `request_id` 与 `control_source`（`AIRPLAY` / `DLNA` / `INTERNAL`）。
由 DLNA 发起、被 AirPlay 回显的状态变化**不会**再次触发同一条 DLNA 动作
（控制回环防护）。

### 3.3 暂停 / 恢复 / Seek

* **AirPlay 暂停**：正常情况下使用真正的 `AVTransport#Pause`，保持 URI / 位置 /
  会话 —— 不把 Pause 当成 Stop。设备是否支持「停留在 PAUSED」由
  `RendererProfile.supports_pause` 表达。
* **恢复**：优先沿用当前会话只发 `Play`（渲染器确实处于 PAUSED 且位置连续时）；
  必要时才换 media session。恢复空窗由输出层静音填充，避免 HTTP EOF。
* **Seek**：AirPlay 的 `pfls`/`pdis` 是唯一媒体生命周期变更来源；Virtual Player 会
  flush 旧 PCM、建立新 generation，绝不让旧位置数据泄漏到新位置。
* **HTTP Range ≠ 媒体 Seek**：实时流一律声明不可寻址并从逻辑起点线性发送
  （`tests/test_stream_multiconn.py` 锁定该行为）。

### 3.4 静音策略

「只要 DLNA 处于播放，HTTP 音频输出就必须连续」：输出层在真实 PCM 不足时生成
静音 PCM 继续供给渲染器，**绝不主动断开响应**。静音超时只通知控制层进入
`RECOVERING`（必要时才执行恢复原语），不会自行断流。静音只存在于 DLNA Output 层，
AirPlay RingBuffer / Timeline 永远只包含真实 AirPlay 音频。

### 3.5 反向控制（DLNA → AirPlay）的真实现状

架构上已实现 DACP 反向控制与逐项能力检测，但**当前环境实测不可用**：

* DACP 需要发送端提供 `acre`（Active-Remote）与 `dapo`（远程控制端口，来自发送端
  广播的 `_dacp._tcp`）。实测局域网内没有任何设备广播 `_dacp._tcp`，
  真机日志里也从未出现 `daid`/`acre`/`dapo`；
* shairport-sync `AIRPLAY2.md` 明确写着 `Remote control facilities are not implemented.`；
* 因此五项能力通常都是 `UNKNOWN`，Virtual Player 只会**如实报告**并保持当前状态与
  当前曲目，绝不伪造状态、绝不自己猜下一首。

结论：在 AirPlay 2 + iPhone 环境下，DLNA 端的 Play/Pause/Next/Previous 只能表现为
渲染器自身的本地行为。**不要**据此认为「反向控制已可用」。

### 3.6 输出缓冲水位

`minimum_buffer_ms` / `target_buffer_ms` / `maximum_buffer_ms`（默认
`400 / 1000 / 1800` 毫秒）可通过 Web UI / API 调整；`target` 参与播放前的预滚动
（`实际预滚动 = max(preroll_seconds, target_buffer_ms/1000)`），**不强制精确 1 秒**。

---

## 4. 目录结构

```text
airplay2-dlna-bridge/
├── TECHNICAL_DESIGN.md      # 技术设计（20 项必答内容）
├── manifest                 # fnOS 包清单
├── ICON.PNG / ICON_256.PNG
├── config/{privilege,resource}
├── cmd/                     # 生命周期脚本（install/upgrade/config/uninstall/main）
├── wizard/{install,config,uninstall}
├── app/
│   ├── ui/                  # Web UI（原生 HTML/CSS/JS）
│   └── server/
│       ├── bridge.py        # 主进程入口
│       ├── config_cli.py    # 生命周期脚本用的配置工具
│       ├── nqptp-watchdog.sh
│       ├── air2dlna/    # Python 包（仅标准库）
│       │   ├── virtual_player.py    # Virtual Player（唯一状态协调中心）
│       │   ├── dlna_output.py       # DLNA Output（AVTransport + 连续 HTTP 输出）
│       │   ├── airplay_remote.py    # DACP 反向控制 + 逐项能力检测
│       │   ├── renderer_profile.py  # Renderer Profile（generic / xiaomi_s12）
│       │   └── state.py             # BridgeController（组合根，转发历史接口）
│       └── bin/  lib/       # 自编译二进制与随包库
├── scripts/build.sh         # 可复现构建
├── tests/                   # 单元测试 + 假渲染器端到端测试
└── docs/ACCEPTANCE.md       # 真机验收清单（Test 1–12）
```

---

## 5. 从源码构建

```bash
./scripts/build.sh              # 全量构建（约 5–10 分钟）
./scripts/build.sh --skip-native  # 只重新打包
# 产物：dist/Air2DLNA-1.0.23.fpk
```

脚本会：安装构建依赖 → 下载并**解包**（不安装）Avahi 开发文件 → 构建最小化静态
FFmpeg（仅 AAC/ALAC）→ 构建 NQPTP → 构建 shairport-sync（AirPlay 2 + avahi）→
收集二进制与随包 `.so` → 用官方 `fnpack` 打包。

> ⚠️ **必须用 Avahi 后端**。shairport-sync 的 `tinysvcmdns` 后端只注册
> `_raop._tcp`，不会注册 AirPlay 2 需要的 `_airplay._tcp`
> （源码核实：`config.regtype2` 只在 `mdns_avahi.c` 中使用）。
> 用错后端会导致 Apple 设备把本机当成 AirPlay 1 设备。
> 构建脚本会校验产物包含 `Avahi` 字样，否则直接失败。

---

## 6. 测试

```bash
# 单元测试（元数据解析、环形缓冲、时间线、SOAP 构造、protocolInfo 选择、配置迁移、
#           Virtual Player 状态机 / 连续输出 / 反向控制能力检测 / 控制回环防护）
python3 -m unittest discover -s tests      # 174 项，全部封闭（无外部网络、无长睡眠）

# 端到端集成测试：真实 SSDP + 真实 SOAP + 真实 HTTP 拉流
# 使用真实线格式的元数据/FIFO 事件驱动，验证到"渲染器收到逐字节一致的 PCM"
python3 tests/integration_test.py          # 42 项检查
```

集成测试会启动 `tests/fake_renderer.py`（一个会真的通过 HTTP 拉流的模拟
MediaRenderer），覆盖：发现 → 选择 → 能力协商 → SetAVTransportURI(DIDL-Lite) →
Play → WAV 头与 PCM 内容校验 → 音量映射 → 暂停/恢复 → Seek 重锚 →
`pend` 过渡态（不立即 Stop）→ `aend` 后 Stop 与状态清理。

`tests/test_virtual_player.py` 另外覆盖 Virtual Player 架构：状态机迁移、请求状态与
实际状态分离、真实 PCM ↔ 静音切换、**不断流保证**、静音不污染 AirPlay 环形缓冲、
逐项反向能力检测（本地假 DACP HTTP 服务器）与「绝不伪造状态」、控制回环防护、
Renderer Profile 选择与覆盖。

---

## 7. 运行数据与日志

| 内容 | 路径 |
|---|---|
| 配置 | `/vol1/@appconf/air2dlna/config.json` |
| 应用日志（结构化，带轮转） | `/vol1/@appdata/air2dlna/bridge.log` |
| 生命周期日志 | `/vol1/@appdata/air2dlna/main.log` |
| 进程 stdout/stderr（启动异常兜底） | `/vol1/@appdata/air2dlna/bridge-stderr.log` |
| 接收器日志 | `/vol1/@appdata/air2dlna/shairport-sync.log` |
| 时钟守护进程日志 | `/vol1/@appdata/air2dlna/nqptp.log` |
| PCM / 元数据 FIFO | `/vol1/@appdata/air2dlna/{audio,metadata}.fifo` |

**日志不会无限增长**：所有日志都有上限 —— 单个文件超过 **5 MB** 时自动只保留
**尾部 1 MB**（原地重写，对正在写入的进程完全无感）。

- `bridge.log`：标准库 `RotatingFileHandler`（5 MB × 5 个备份）
- `main.log` / `bridge-stderr.log`：启动与停止时轮转，并由 nqptp 看护脚本每 60 秒检查
- `shairport-sync.log`：由进程监管循环每 60 秒检查
- `nqptp.log`：由看护脚本每 60 秒检查

升级**不会**删除配置；卸载时可在向导里选择保留或删除。

### 诊断日志

排查播放问题时优先看这几行（Web UI 的日志页或 `bridge.log`）：

| 日志 | 含义 |
|---|---|
| `SetAVTransportURI #N reason=... generation=...` | 第 N 次更换媒体资源及原因。**一次正常播放应当只有 1 次**（`reason=pbeg`），多于 1 次才说明有异常重建 |
| `检测到 seek/flush (...) 换代并重锚 DLNA 会话` | 真实 seek（`pfls`/`pdis`）：允许且预期一次换代 |
| `AirPlay 播放流结束（...）：进入过渡态` | 收到 `pend`。DLNA 侧保持不动等待新的流，不再立即停止 |
| `过渡态结束（...）：继续沿用当前 DLNA 会话` | 新流在窗口内到达 → 播放继续（拖动进度条后的正常路径） |
| `过渡态超时（...），按播放结束处理` | 窗口内确实没有新流，才真正停止 |
| `渲染器 Ns 未拉取音频流（...），重建 DLNA 会话` | 渲染器链路真的断了 → 兜底重建 |
| `诊断: airplay_pos=... rel_time=... offset=... rate=... gen=... http_clients=... uri_count=...` | 每 15 秒一条。`offset` 是渲染器位置与 AirPlay 时间线的偏差，**稳定的数千毫秒属于正常缓冲延迟**；`rate` 明显偏离 1.0 才可能是真时钟漂移 |
| `渲染器时钟速率偏离（ΔRelTime/ΔAirPlay=...）：仅记录诊断，不重建会话` | 真漂移的诊断记录（1.0.5 起不再自动重建） |

### 常用排查

```bash
appcenter-cli status air2dlna          # 运行状态
tail -f /vol1/@appdata/air2dlna/bridge.log
avahi-browse -rt _airplay._tcp             # 应能看到你的 AirPlay 名称与 features=...
ss -lunp | grep -E ':319|:320'             # NQPTP 的 PTP 端口
```

若 iPhone 看不到设备：确认 `_airplay._tcp` 已注册、NAS 与手机在同一 LAN、
且 7000/319/320 未被其它 AirPlay 应用（如 micast / juneix.airplay2）占用。

---

## 8. 已知限制（第一阶段）

* 只支持**一个** AirPlay 接收器 → **一个** DLNA 渲染器；不做多房间同步
  （架构已预留，见设计文档第 14 节）。
* 输出格式为 WAV/LPCM；不支持 FLAC 输出（原因见设计文档第 5 节）。
* 实时流不做字节范围寻址，Seek 通过「换代 + 新 URI」实现（听感正确、状态重锚）。
* **反向控制（DLNA → AirPlay）在当前环境不可用**：AirPlay 2 发送端不提供 DACP
  凭据（`daid`/`acre`/`dapo`），shairport-sync 也明确不实现远程控制。架构上已支持
  并会如实检测能力，但 DLNA 端的 Play/Pause/Next/Previous 目前只能表现为渲染器
  自身的本地行为，Virtual Player 不会伪造状态（详见第 3.5 节）。
* 连续输出、`xiaomi_s12` Profile 的 Stop/Pause 策略与缓冲水位取值仍需在真机
  （小爱音箱 S12 + iPhone）上验证；`tests/` 中覆盖的是封闭的逻辑层测试。
* 依赖飞牛自带的 `avahi-daemon` 与 D-Bus 来完成 mDNS 注册。
