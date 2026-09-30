Air2DLNA Virtual Player 架构重构与新版本发布规格

0. 任务目标

当前 Air2DLNA 已经能够完成：

iPhone / iPad / Mac
        ↓ AirPlay 2
shairport-sync + NQPTP
        ↓
Air2DLNA
        ↓ DLNA / HTTP
Xiaomi 小爱音箱 S12

现有版本已经解决了基本播放、暂停恢复、HTTP 重连、Timeline 等问题，但当前架构仍然偏向：

AirPlay Event
    ↓
直接映射 DLNA Action

这种架构导致 Pause / Resume / Seek / Next / Previous / HTTP 生命周期之间耦合较强，并且大量逻辑是在针对 S12 的具体异常行为做补丁。

本版本要求进行一次架构级调整：

«将 Air2DLNA 从“AirPlay → DLNA 音频转发器”重构为“AirPlay → Virtual Player → DLNA Renderer”的虚拟播放器架构。»

核心原则：

«AirPlay 是 Playback Authority（播放状态权威）
Virtual Player 是 State Coordinator（播放状态协调器）
DLNA Renderer 是 Output Renderer（音频输出设备）»

不要继续围绕现有的 "new generation + SetURI + Play" 逻辑不断打补丁，而应该将这些逻辑收敛到新的 Virtual Player / DLNA Output 层。

---

1. 总体架构

目标架构：

                         ┌──────────────────┐
                         │      iPhone      │
                         │   AirPlay Source │
                         └────────┬─────────┘
                                  │
                          AirPlay Audio
                         + State Events
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │    VIRTUAL PLAYER   │
                       │                     │
                       │ Track               │
                       │ Position            │
                       │ Duration            │
                       │ Playback State      │
                       │ Timeline            │
                       │ Audio Buffer        │
                       │ Control Coordinator │
                       └──────────┬──────────┘
                                  │
                         Virtual Media Resource
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │    DLNA OUTPUT      │
                       │                     │
                       │ AVTransport         │
                       │ HTTP Stream         │
                       │ Renderer Profile    │
                       └──────────┬──────────┘
                                  │
                                  ▼
                              Xiaomi S12

反向控制：

S12 / DLNA Controller
        │
        │ Play / Pause / Seek / Next / Previous
        ▼
Virtual Player
        │
        │ AirPlay Remote Control
        ▼
iPhone / AirPlay Source
        │
        │ 实际状态变化
        ▼
Virtual Player
        │
        ▼
DLNA Output

---

2. 最重要的架构原则

2.1 AirPlay 是播放状态权威

Virtual Player 不自行创造“真实播放状态”。

AirPlay 发生：

PLAY
PAUSE
SEEK
TRACK CHANGE
STOP

后，Virtual Player 根据 AirPlay 的实际状态更新自己的状态。

---

2.2 DLNA 控制只是“用户请求”

当 DLNA 端发生：

Play
Pause
Seek
Next
Previous

Virtual Player 不应该直接改变播放状态。

例如：

DLNA Pause
    ↓
Virtual Player
    ↓
AirPlay Remote Pause
    ↓
等待 AirPlay 实际进入 PAUSED
    ↓
Virtual Player = PAUSED
    ↓
同步 DLNA

禁止：

DLNA Pause
    ↓
直接停止 PCM
    ↓
Virtual Player = PAUSED

否则可能产生：

iPhone = PLAYING
Virtual Player = PAUSED
S12 = PAUSED

状态分裂。

---

3. Virtual Player 必须成为唯一状态协调中心

Virtual Player 至少维护：

current_track
track_id / track_identity
track_metadata

playback_state
playback_position
duration

audio_buffer
buffer_level

timeline

pending_control
control_source
request_id

dlna_session
airplay_session

建议状态：

IDLE
BUFFERING
PLAYING
PAUSED
SEEKING
TRACK_SWITCHING
RECOVERING
STOPPING

控制请求可以使用独立的 pending 状态：

PLAY_REQUESTED
PAUSE_REQUESTED
SEEK_REQUESTED
NEXT_REQUESTED
PREVIOUS_REQUESTED

不要把“请求状态”和“实际播放状态”混为一谈。

---

4. DLNA Output 与 Virtual Player 解耦

Virtual Player 不应该直接操作：

SetAVTransportURI
Play
Pause
HTTP connection

这些全部交给：

DLNAOutput

Virtual Player 只表达：

PLAY
PAUSE
RESUME
SEEK
TRACK_CHANGED
STOP

DLNAOutput 再把这些状态转换成具体 UPnP/DLNA 操作。

---

5. 小爱收到的媒体资源

DLNA Renderer 应看到一个稳定的 Virtual Media Resource，例如：

/current

或：

/session/<id>

它应该表现得像一个正常的 DLNA 媒体资源。

小爱：

SetAVTransportURI
        ↓
Play
        ↓
HTTP GET
        ↓
Virtual Player Output

小爱不需要知道：

AirPlay
RTP
NQPTP
iPhone
Seek
AirPlay Buffer

---

6. 音频输出必须保持连续

这是本版本非常重要的设计原则：

«DLNA HTTP Output 是连续媒体时钟；真实 AirPlay PCM 只是这个时钟上的真实音频内容。»

当 Virtual Player 有真实 PCM：

真实 AirPlay PCM
        ↓
DLNA HTTP

当暂时没有真实 PCM：

Virtual Player
        ↓
Silence PCM
        ↓
DLNA HTTP

因此：

真实 PCM → 真实 PCM → Silence → Silence → 真实 PCM

而不是：

真实 PCM → EOF / disconnect → 重新连接

---

7. Silence PCM 的用途

以下场景都可能暂时没有真实 AirPlay PCM：

Resume

AirPlay Resume
      ↓
DLNA Play
      ↓
AirPlay PCM 尚未恢复
      ↓
输出 Silence
      ↓
真实 PCM 到达
      ↓
切换到真实 PCM

Seek

Seek
 ↓
清空旧 Buffer
 ↓
等待新位置 PCM
 ↓
Silence
 ↓
新位置 PCM

Next / Previous

Track A
 ↓
TRACK_SWITCHING
 ↓
等待 Track B PCM
 ↓
Silence
 ↓
Track B PCM

Silence 必须在 DLNA Output 层生成。

禁止把 Silence 写入 AirPlay RingBuffer 或 AirPlay Timeline。

AirPlay Timeline 永远只代表真实 AirPlay 音频。

---

8. Silence 必须有超时机制

不能无限输出 Silence。

建议：

normal buffering:
    target ≈ 1s

silence/recovery timeout:
    configurable

如果长时间没有真实 AirPlay PCM：

Silence
 ↓
timeout
 ↓
RECOVERING

检查：

AirPlay session
AirPlay state
DLNA renderer state
HTTP connection
buffer

必要时执行现有可靠恢复方案：

new generation
+
SetAVTransportURI
+
Play

现有恢复机制可以保留作为 fallback，但不再作为正常播放流程。

---

9. 1 秒 Buffer

Virtual Player 建立独立的输出 Buffer。

默认：

target_buffer ≈ 1.0s

不要强制要求永远精确 1 秒。

建议支持：

minimum_buffer
target_buffer
maximum_buffer

例如：

minimum ≈ 300~500ms
target ≈ 1000ms
maximum ≈ 1500~2000ms

具体参数根据实际测试调整。

目标：

«在不明显增加延迟的情况下，吸收 AirPlay → Virtual Player → DLNA 之间的短暂抖动。»

---

10. AirPlay 正向播放流程

Play

AirPlay PLAY
    ↓
Virtual Player
    ↓
BUFFERING
    ↓
收到足够 PCM
    ↓
DLNAOutput CONNECT
    ↓
SetAVTransportURI
    ↓
Play
    ↓
PLAYING

如果 DLNA 已经处于可用播放状态，则不要无意义地重新 SetURI。

---

11. AirPlay Pause

AirPlay PAUSE
      ↓
Virtual Player = PAUSED
      ↓
DLNAOutput.Pause()

正常情况下优先使用真正的：

AVTransport.Pause

并尽量保持：

URI
Position
Media Session

不要把 Pause 当成 Stop。

如果 S12 的 Pause 会导致 HTTP pipeline 关闭，则由 DLNA Renderer Profile / Output 层处理兼容。

---

12. AirPlay Resume

AirPlay RESUME
      ↓
Virtual Player = BUFFERING / RESUMING
      ↓
DLNA Play
      ↓
如果真实 PCM 尚未到达
      ↓
输出 Silence
      ↓
PCM 到达
      ↓
真实 PCM
      ↓
PLAYING

目标是避免：

Resume
 ↓
短暂无 PCM
 ↓
HTTP EOF
 ↓
S12 STOPPED

---

13. AirPlay Seek

AirPlay Seek 是 Virtual Player 的时间轴操作。

不要简单地：

AirPlay Seek
 ↓
直接 DLNA Seek

推荐：

AirPlay Seek(target)
        ↓
Virtual Player = SEEKING
        ↓
停止向 DLNA 输出旧 PCM
        ↓
Flush Virtual Player Buffer
        ↓
请求 AirPlay 到 target
        ↓
等待新位置 PCM
        ↓
缓存约 1s
        ↓
建立新的 Virtual Media Session / 必要时 SetURI
        ↓
DLNA Play
        ↓
输出新位置 PCM

关键：

«Seek 后绝对不能让旧位置的 PCM 从 Buffer 泄漏到新位置。»

DLNA 不需要理解 AirPlay 的内部 Seek 时间轴。

---

14. DLNA → AirPlay 反向 Pause

这是本版本必须支持的核心逻辑。

DLNA Pause Request
        ↓
Virtual Player
        ↓
AirPlay Remote Pause
        ↓
等待 AirPlay 实际 PAUSED
        ↓
Virtual Player = PAUSED
        ↓
DLNA 状态同步

禁止：

DLNA Pause
 ↓
直接停止 PCM

因为 AirPlay 仍可能处于 PLAYING。

---

15. DLNA → AirPlay 反向 Play

DLNA Play Request
        ↓
Virtual Player
        ↓
AirPlay Remote Play
        ↓
等待 AirPlay 恢复
        ↓
Virtual Player BUFFERING
        ↓
PCM 到达
        ↓
DLNA Output 持续输出
        ↓
PLAYING

没有 PCM 的短暂阶段使用 Silence。

---

16. DLNA → AirPlay Seek

DLNA Seek(target)
        ↓
Virtual Player
        ↓
AirPlay Remote Seek(target)
        ↓
等待 AirPlay 新位置
        ↓
Flush Buffer
        ↓
重新 Buffer
        ↓
DLNA Output

不要由 Virtual Player 自己猜测下一段音频的位置。

---

17. DLNA → AirPlay Next / Previous

这是非常重要的设计：

«Virtual Player 不自己寻找下一首歌。»

例如：

S12 Next
   ↓
Virtual Player
   ↓
AirPlay Remote Next
   ↓
iPhone 音乐 App
   ↓
真正切换歌曲
   ↓
AirPlay 新 Track
   ↓
Virtual Player

Virtual Player：

TRACK_SWITCHING
 ↓
Flush 当前 Track Buffer
 ↓
等待新 Track
 ↓
缓存约 1s
 ↓
输出 Silence
 ↓
新 Track PCM
 ↓
PLAYING

Previous 同理。

---

18. Track Identity

Virtual Player 必须明确维护：

track_id
title
artist
album
duration
metadata

AirPlay Track 变化时：

Track A
 ↓
TRACK_SWITCHING
 ↓
Track B

不要仅仅根据 PCM 内容变化判断切歌。

如果当前 AirPlay/Shairport Sync 能提供可靠 Track/metadata 事件，优先使用。

---

19. 防止控制回环

必须处理：

DLNA Pause
 ↓
AirPlay Remote Pause
 ↓
AirPlay PAUSED
 ↓
同步 DLNA

不能再次触发：

DLNA Pause
 ↓
AirPlay Remote Pause
 ↓
...

建议控制请求携带：

request_id
control_source

例如：

control_source:
    AIRPLAY
    DLNA
    INTERNAL

状态变化和控制请求必须区分。

---

20. Remote Control 能力检测

不要假设 AirPlay Remote Control 的所有功能都可用。

独立实现：

AirPlayRemoteController

能力：

play
pause
seek
next
previous

每项记录：

SUPPORTED
UNSUPPORTED
UNKNOWN

如果某项无法执行：

«不允许 Virtual Player 假装状态已经改变。»

例如：

DLNA Next
 ↓
AirPlay Remote Next failed
 ↓
保持当前 Track

不要自己猜下一首。

---

21. DLNA Renderer Profile

不同 DLNA Renderer 的行为可能不同。

增加 Renderer Profile：

renderer_profile:
    persistent_stream
    supports_pause
    pause_keeps_uri
    pause_keeps_http
    supports_range
    supports_seek
    reconnect_behavior
    buffer_behavior

S12 可以作为一个专门 Profile：

xiaomi_s12

但不要把 S12 的 workaround 写死到 Virtual Player 核心。

---

22. 保留现有 Timeline

现有 AirPlay Timeline / RTP + monotonic anchor 逻辑已经验证有效。

原则：

«不要因为 Virtual Player 重构而重新设计 AirPlay Timeline。»

Timeline 继续作为：

AirPlay authoritative timeline

Virtual Player 可以在其上建立：

output position
buffer position

但不要污染 AirPlay Timeline。

---

23. RingBuffer

保留现有 RingBuffer。

但职责明确：

AirPlay RingBuffer
=
真实 AirPlay PCM 的缓存

不要往里面写：

Silence

Silence 只在：

DLNA Output / HTTP Output

层生成。

---

24. HTTP Stream

现有实时 WAV/LPCM HTTP Stream 可以继续使用。

但重新定义为：

Virtual Media Output

它必须能够：

read real PCM
read silence
switch real/silence
handle reconnect
handle generation/session

HTTP Client 短暂重新连接时，不应该直接导致 Virtual Player 状态异常。

---

25. 关于 HTTP Range

不要再把：

HTTP Range

理解成：

媒体 Seek

Range 只是 Renderer HTTP 层面的数据请求行为。

真正的媒体 Seek：

AirPlay / Virtual Player Timeline

负责。

如果 Renderer 重新 GET：

Range: bytes=0-

也不应该因此把 Virtual Player Seek 到 0。

---

26. 正常流程与 Recovery 必须分开

正常：

AirPlay
 ↓
Virtual Player
 ↓
DLNA Output
 ↓
S12

Recovery：

异常
 ↓
RECOVERING
 ↓
必要时：
new generation
SetURI
Play
 ↓
恢复正常 Virtual Player

不要让 Recovery 逻辑成为正常 Resume / Seek / Pause 的主要路径。

---

27. 不要修改以下已经验证有效的部分

除非新架构确实需要，否则不要大幅修改：

shairport-sync 5.5.1
NQPTP
AirPlay 2 接收
mDNS
SSDP / UPnP discovery
ConnectionManager
现有 AirPlay RingBuffer 核心
RTP Timeline
现有音量控制
现有基础 Metadata

本次重点是架构层重新组织，不是重新实现 AirPlay Receiver。

---

28. 不要做这些事情

本版本明确禁止以下方向：

不要：
- 通过 drift threshold 周期性修正
- 周期性 Stop/Play
- 周期性重新 SetURI
- 把 Silence 写入 AirPlay RingBuffer
- 把 Silence 写入 AirPlay Timeline
- 用 HTTP Range 推断媒体 Seek
- 假造 DIDL duration 来掩盖问题
- 通过修改 AirPlay feature bits 绕过问题
- 让 DLNA 自己决定下一首歌
- DLNA Pause 时直接停止 AirPlay PCM
- DLNA Next/Previous 时自己猜歌曲

---

29. 测试要求

新版本必须至少验证：

基础播放

AirPlay Play
→ S12 出声

AirPlay Pause / Resume

Play
Pause 1s
Resume

Pause 5s
Resume

Pause 30s
Resume

Pause 60s
Resume

记录：

Resume → 实际出声

目标：

«尽可能接近正常 DLNA 播放器的恢复速度。»

---

Resume 无 PCM

人为制造：

DLNA 已 Play
AirPlay PCM 延迟到达

确认：

没有真实 PCM
→ 输出 Silence
→ HTTP 不断
→ S12 不进入 STOPPED
→ PCM 到达后自动恢复真实音频

---

Seek

测试：

00:30 → 02:00
02:00 → 00:10
00:10 → 04:00

确认：

旧位置 PCM 不泄漏
新位置正常播放
S12 不因短暂无 PCM 而断流

---

DLNA → AirPlay Pause

iPhone PLAYING
 ↓
S12 Pause
 ↓
AirPlay Remote Pause
 ↓
iPhone 真正暂停

确认三个状态最终一致。

---

DLNA → AirPlay Resume

S12 Play
 ↓
AirPlay Remote Play
 ↓
iPhone Resume
 ↓
PCM
 ↓
S12

---

DLNA → AirPlay Next

Track A
 ↓
S12 Next
 ↓
AirPlay Remote Next
 ↓
iPhone 切 Track B
 ↓
Virtual Player Buffer
 ↓
Track B 出声

---

DLNA → AirPlay Previous

同上。

---

连续操作

至少测试：

Pause
Resume
Pause
Resume
Seek
Pause
Resume
Next
Previous
Next

确认不存在：

状态死锁
控制回环
HTTP断流
旧PCM泄漏
状态不同步

---

30. 发布要求

完成后：

1. 更新版本号。
2. 更新 README 架构说明。
3. 更新 CHANGELOG。
4. 明确说明 Virtual Player 架构。
5. 记录 S12 的 Renderer Profile。
6. 编译新的 fnOS FPK。
7. 完成基本功能测试。
8. 发布新的版本。

版本号按照当前项目版本顺序递增。

---

31. 最终验收标准

最终希望达到：

             ┌───────────────────────┐
             │        AirPlay        │
             │                       │
             │  Playback Authority   │
             └───────────┬───────────┘
                         │
                         ▼
             ┌───────────────────────┐
             │    Virtual Player     │
             │                       │
             │ Track                 │
             │ Timeline              │
             │ Buffer                │
             │ State                 │
             │ Remote Control        │
             └───────────┬───────────┘
                         │
                         ▼
             ┌───────────────────────┐
             │      DLNA Output      │
             │                       │
             │ AVTransport           │
             │ Continuous HTTP       │
             │ Renderer Profile      │
             └───────────┬───────────┘
                         │
                         ▼
                       S12

核心行为：

«AirPlay 决定“实际播放状态”。»

«Virtual Player 负责把 AirPlay Session 虚拟成一个正常的播放器。»

«DLNA 负责把这个虚拟播放器呈现给小爱。»

«小爱端的 Play / Pause / Seek / Next / Previous 都只是控制请求，最终必须反向作用到 AirPlay。»

«只要 DLNA 处于 PLAYING，HTTP 音频输出就必须保持连续；暂时没有真实 AirPlay PCM 时，用 Silence PCM 填充，不能因为短暂无 PCM 主动断流。»

«Silence 只存在于 DLNA Output 层，不得污染 AirPlay RingBuffer 和 Timeline。»

这套架构完成后，Air2DLNA 不再是简单的：

AirPlay → DLNA

而是：

AirPlay Session
       ↕
Virtual Player
       ↕
DLNA Renderer

最终目标是让 S12 感觉自己连接的是一个正常、稳定、可暂停、可恢复、可 Seek、可切歌的 DLNA 播放器，而不是一个 AirPlay 音频转发器。