# Air2DLNA 「拖动进度条后无声」问题分析材料

> 用途：把这份材料整体交给 ChatGPT（或其他人）分析。
> 内容全部来自真机日志、代码核对与实测数据；已明确标注哪些是**实测**、哪些是**推测**。
> 项目仓库：https://github.com/youyoudezhuzhu/Air2DLNA

---

## 一、系统是什么

把 iPhone 的 AirPlay 2 音频转发到**只能通过 DLNA/UPnP 播放**的音箱（小米小爱音箱 S12）。

```
iPhone (AirPlay 2 发送端)
   │  AirPlay 2 / RTSP + RTP
   ▼
shairport-sync 5.5.1 (--with-airplay-2)  +  NQPTP (时间同步)
   │  解码为 S16LE / 44100Hz / 2ch PCM，写入命名管道
   ▼
Air2DLNA 桥接进程（Python，本项目）
   │  把 PCM 写入环形缓冲 → 通过 HTTP 提供**实时 WAV 流**
   │  同时用 UPnP AVTransport 控制音箱
   ▼
小爱音箱 S12（DLNA MediaRenderer，HTTP 拉流播放）
```

### 关键设计（当前版本 1.0.25/1.0.26）

* **环形缓冲 + 「代（generation）」机制**：每次 seek / 换曲 / 会话重建，都会
  `ring.flush()` 新开一代，并生成新的 HTTP URI `/stream/<时间戳>-<代>.wav`。
* **连续输出**：只要 DLNA 处于播放态，HTTP 响应**绝不 EOF**；暂时没有真实 PCM 时，
  输出层按实时速率发送**静音 PCM** 填充空窗。静音只在输出层生成，
  **不写入**环形缓冲/时间线。
* **Renderer Profile（设备档案）**：按设备身份选择策略。真机 S12 的档案为：

  | 字段 | 值 | 依据 |
  |---|---|---|
  | `supports_pause` | `False` | 真机日志：对它调 `AVTransport#Pause` 后它会自行进入 `STOPPED` |
  | `pause_keeps_uri` | `False` | 同上 |
  | `pause_keeps_http` | `False` | 暂停后它丢弃 HTTP 拉流 |
  | `resume_requires_reannounce` | `True` | 因为它已 STOPPED，旧 URI 上裸发 `Play` 无效 |

* **反向控制（DLNA→AirPlay）不可用**：发送端不提供 DACP 凭据
  （局域网 `avahi-browse -rt _dacp._tcp` 无任何广播；日志从未出现 `daid`/`acre`/`dapo`；
  shairport-sync `AIRPLAY2.md` 明确写 “Remote control facilities are not implemented”）。
  因此音箱端的 Play/Pause/Next 只是它的本地行为，无法作用到 iPhone。

---

## 二、症状

| 操作 | 现象 |
|---|---|
| 播放 | 正常 |
| 暂停 → 恢复 | 约 3 秒延迟（从早前的 5~6 秒改善而来） |
| **拖动进度条（seek）** | **音箱完全没有声音，且不再恢复** |

「seek」指在 iPhone 上拖动播放进度条。

---

## 三、最关键的一组实测数据

**换代 → `SetAVTransportURI` 的延迟**（同一版本 1.0.25 内测得）：

| SetURI # | 触发原因 | 代 | 换代 → SetURI 延迟 |
|---|---|---|---|
| #1 | `resume-rebuild` | 2 | 2 秒 |
| #2 | `resume-rebuild` | 3 | 1 秒 |
| **#3** | **`resume-rebuild`（seek）** | 4 | **8 秒** |
| **#4** | **`resume-rebuild`（seek）** | 5 | **8 秒** |
| #1 | `pause-recovery-unsupported` | 7 | 1 秒 |

**两次 seek 都精确地吃满 8.0 秒**（不是 1~2 秒），这个数字与代码里的预滚动超时完全吻合：

```python
# dlna_output.py, do_play(), 情况 3
needed = int(plan["target_seconds"] * self.ring.byte_rate)   # preroll_seconds = 2.0 → 2 秒数据
ready = self.ring.wait_for_data(0, generation, needed, timeout=8.0)   # ← 最多阻塞 8 秒
...
# 只有等待结束后，才发出 SetAVTransportURI
```

日志同时给出证据：等待结束时**可用数据是 0**。

```
21:26:24 WARNING [dlna_output] 预滚动等待不足（需要 2.0s，实际 0.0s），仍尝试开始播放
```

**含义**：seek 之后 iPhone 要 10~20 秒才会重新送出音频（见下节），
而我们在这段等待里**整整阻塞 8 秒才去告诉音箱「换个 URI 播放」**。
这 8 秒内音箱处于 `STOPPED`、用户完全无声。

> 已在 1.0.26 中修改：连续输出模式下**不再等待预滚动，立即宣告 URI**
> （因为输出层本来就会用静音填充空窗，等待只会白白增加延迟）。

---

## 四、一次 seek 的完整真机时序（1.0.24）

```
21:26:16  ← 用户拖动进度条
         Profile xiaomi_s12: supports_pause=False → 直接使用 Stop
         恢复播放：渲染器此前无法暂停（Profile 声明），重建 DLNA 会话
         gen=4，起始曲目位置 54389 ms
         seek 后重锚：新的曲目位置约为 142242 ms        ← 用户拖到 142 秒处
21:26:16 渲染器开始拉流: token=...-3 gen=4 range_start=2686904 (第 2 次连接)
         忽略 Range 偏移（实时流不可寻址，改从当前起点线性发送）
         请求的 token 已被换代取代 → 按实时流语义改为输出最新一代
         连续输出：真实 PCM 暂时不足，切换为静音填充
21:26:17 诊断: airplay_pos=141943 state=BUFFERING renderer=STOPPED rel_time=-
21:26:24 预滚动等待不足（需要 2.0s，实际 0.0s），仍尝试开始播放      ← 8 秒过去，0 数据
21:26:24 SetAVTransportURI #3 reason=resume-rebuild gen=4
21:26:24 渲染器开始拉流: token=...-4 gen=4 range_start=0 (第 1 次连接)
21:26:24 连续输出：真实 PCM 暂时不足，切换为静音填充
21:26:24 WARNING 连续输出：真实 PCM 已中断 8.1s（阈值 8.0s）
21:26:25 WARNING 连续输出静音超时：进入 RECOVERING（渲染器=PLAYING 拉流连接=1）
         静音超时但渲染器仍在拉流：继续用静音保持输出，等待 AirPlay 数据恢复
21:26:33 AirPlay 事件: pend（播放流结束）              ← ★ 拖动 17 秒后才收到 pend
21:26:33 AirPlay session started                       ← 新会话
21:26:33 过渡态结束（pbeg）：继续沿用当前 DLNA 会话 gen=4
21:26:33 pbeg：曲目位置连续（变化 0 ms），沿用当前 DLNA 会话
21:26:34 AirPlay 事件: prsm（播放流已恢复）
21:26:34 AirPlay 流类型: Buffered
21:26:34 连续输出：真实 PCM 恢复，结束静音填充（空窗 0.2s）   ← ★ 21:26:34 才有 PCM（距拖动 18 秒）
21:26:34 WARNING 连续输出静音超时 → RECOVERING（渲染器=STOPPED 拉流连接=0）  ← ★ 渲染器又变 STOPPED
21:26:43 连续输出：真实 PCM 暂时不足，切换为静音填充
21:26:52 诊断: airplay_pos=10384 state=PAUSED machine=PAUSED renderer=STOPPED
21:26:53 AirPlay session ended (播放结束)
21:27:10 诊断: state=STOPPED machine=IDLE
（此后一直 IDLE，用户听到的是彻底的静音）
```

### 从这段日志可以读出的事实

1. **拖动进度条后，iPhone 侧有约 17~18 秒的「重新建流 + 重缓冲」空窗**
   （21:26:16 拖动 → 21:26:33 `pend` → 21:26:34 首个 PCM）。
   这段时间内 shairport-sync **一个字节 PCM 都没有产出**（`实际 0.0s`）。
2. **我们的 8 秒预滚动等待正好落在这个空窗里**，把 SetAVTransportURI 推后到 21:26:24。
3. **PCM 于 21:26:34 到达后，渲染器随即又变成 `STOPPED` / `拉流连接=0`**，然后整个会话结束。
4. **位置数值非常不稳定**：`58388 → 126556 → 132971 → 656 → 1045 → 10384`。
   同一个「曲目位置」在同一分钟内可以跳到几百毫秒量级，也可以跳到十几万毫秒。
5. `pend` 在 seek 之后**迟到 17 秒**才到，而 `pbeg` 紧跟其后 —— 我们此前依赖
   `pbeg`/`pend` 判断「是否换曲 / 是否重建」，在这种迟到 + 位置乱跳的组合下容易误判。

---

## 五、已经修掉的问题（都已实测确认，可排除）

按版本倒序，每一项都有日志或测试证据：

| 版本 | 问题 | 状态 |
|---|---|---|
| 1.0.22-1 | HTTP `Range` 被当成媒体 seek 处理（导致阻塞与错位） | 已修，测试锁定 |
| 1.0.22-1 | 同一个流 token 被多次 GET 后，后续连接只返回 44 字节 WAV 头（0 字节 PCM） | 已修，测试锁定 |
| 1.0.24 | 预滚动期间 `StaleGeneration` 抛出 `converge`，导致 `SetURI`/`Play` 根本没发出 | 已修 |
| 1.0.24 | 换代后旧 token 只发 0 字节（`closed` 被当成「该断开」） | 已修 |
| 1.0.24 | Renderer Profile 按 UDN 永久缓存，`model=''` 时选中 `generic` 后不再重算 | 已修 |
| 1.0.24 | 对「暂停实为 Stop」的设备仍启用 keepalive（造成 5~6 秒延迟） | 已修（Profile 一票否决） |
| **1.0.26** | **连续输出下预滚动等待阻塞 SetAVTransportURI 达 8 秒** | **本次修复** |

### 我在 1.0.25 里引入的**回退**（必须说明）

1.0.25 我收紧了 `_can_reuse_generation()`：要求渲染器「非 STOPPED 且确有 HTTP 客户端在拉流」，
并对 `resume_requires_reannounce=True` 的设备一律不沿用会话。**方向是对的**，
但它使**换代更频繁**，而每次换代在 seek 时都要吃满 8 秒预滚动等待 ——
两个改动叠加，**整体变得更慢**，这正是用户反馈「1.0.25 还不如 1.0.24」的原因。
1.0.26 去掉 8 秒阻塞后，该叠加效应消失。

---

## 六、当前仍然存在的疑点（按可能性排序）

### 假设 1（高）：seek 后我们主动 `Stop` 了渲染器，逼它彻底重缓冲
S12 档案规定 `supports_pause=False → 直接使用 Stop`。seek 伴随一次暂停语义，
于是我们给音箱发 `AVTransport#Stop`，**主动拆掉它的 HTTP 连接和缓冲**，随后又要求它
重新 `SetURI` + `Play`。而音箱此刻拿不到真实 PCM（只有我们填充的静音）。

* **可验证**：seek 时改成**不发 Stop**，只切换 generation、保持会话与 HTTP 连接不变，
  靠连续输出的静音撑住；观察是否还无声。
* 相关代码：`dlna_output.py` 中响应 Profile 的 pause 分支；`virtual_player._handle_pause()`

### 假设 2（高）：音箱拿到「只有静音」的流之后自己停了
时序显示：`连续输出：真实 PCM 恢复（空窗 0.2s）` 之后**紧接着**渲染器变成
`STOPPED / 拉流连接=0`。也就是说 PCM 刚接上，它反而停了。

* 可能性：音箱对「先长时间静音、再出声音」这种流有自己的超时/判废逻辑；
  或者它认为我们声明的 `Content-Length` / DIDL 元数据与实际不符而放弃。
* **可验证**：把静音填充改为「不发静音、直接挂住 HTTP 不返回数据」（或反过来
  完全不填充，观察差异）；对比音箱行为。

### 假设 3（中高）：S12 根本不支持我们要用的流格式
真机探测结果（`/api/status`）：

```
model = S12
manufacturer = Mi, Inc.
supported_mime = None
capability_error = Renderer does not support required MIME type (需要 audio/wav 或 audio/L16)
```

音箱的 SSDP `sink` 字段是 `http-get:*:*:*` —— **`*` 表示它没有声明任何可用 MIME**。
我们目前是「保守回退到 WAV」硬发。它的一系列异常行为
（Pause 变成 Stop、频繁 `Range` 探测、`RelTime` 不可信、seek 后判废）
都与「这台设备其实没有正经实现 HTTP 流媒体」相符。

* **可验证**：改用 `audio/L16`（裸 PCM）或调整 `output_format`；或换一台
  DLNA 音箱做对照 —— 如果另一台设备 seek 正常，就说明问题在 S12 的兼容性，
  而不在我们的状态机。

### 假设 4（中）：`av_offset_ms = 6415` 污染了位置/漂移计算
真机配置里 `av_offset_ms = 6415`（6.4 秒），这是一个**手工填的**值，直接进入
位置与漂移公式。结合上面「位置在 656 ~ 132971 之间乱跳」的现象，
这个偏移很可能是错误标定的。

* **可验证**：设为 `0` 再测一遍。

### 假设 5（中）：`pend` 迟到 + 位置乱跳导致状态机误判
seek 后 `pend` 迟到 17 秒、`pbeg` 紧随其后，且位置数值乱跳。
我们当前用「位置是否连续」判断「沿用会话 vs 重新宣告」，
在这种输入下容易作出错误决策（1.0.24 就是因此「沿用」了一个渲染器已 STOPPED 的会话）。

* **可验证**：把决策依据从「位置连续性」改为「渲染器实际可播放性 + 是否真有客户端在拉流」
  （1.0.25 已部分这么做），并观察。

### 假设 6（低）：静音填充让音箱认为「该曲目已结束」
音箱可能有「长时间无有效音频即判结束」的行为。

---

## 七、环境与配置事实（供分析参考）

**真机配置**（`/vol1/@appconf/air2dlna/config.json`，节选）：

```json
{
  "preroll_seconds": 2.0,
  "target_buffer_ms": 1000,
  "minimum_buffer_ms": 400,
  "maximum_buffer_ms": 1800,
  "av_offset_ms": 6415,
  "recovery_mode": "keepalive",
  "continuous_output": true,
  "silence_timeout_seconds": 8.0,
  "metadata_poll_seconds": 3.0,
  "sample_rate": 44100,
  "channels": 2,
  "output_format": "auto",
  "renderer_profile": ""
}
```

* 音箱：小爱音箱 S12，`model=S12`，`manufacturer=Mi, Inc.`，
  UDN `uuid:64f2215e-dc4f-4680-9b2d-e8699f4c43ad`，声明的 sink = `http-get:*:*:*`
* 发送端：iPhone，AirPlay 2，流类型报告为 `Buffered`
* 主机：飞牛 OS（fnOS），Debian 12 内核 6.18，应用以非 root 用户运行
* shairport-sync 5.5.1、NQPTP 1.2.8
* 时间线参考：`offset=6415`、`rate=1.0`、渲染器 `RelTime` 经常为 `-` 或很小的值

---

## 八、希望得到的分析 / 建议

1. **seek 之后音箱彻底无声，最可能的根因是什么？** 我们的状态机、HTTP 供给方式、
   还是 S12 自身对流媒体的兼容性？
2. DJLNA `AVTransport` 的**正确 seek 语义**是什么？规范里 seek 应该用
   `AVTransport#Seek` 还是 `Stop + SetAVTransportURI + Play`？
   对一个「不能可靠 Pause、会自行 STOPPED」的渲染器，业界（如 AirConnect、
   gmrender、miniDLNA 桥接方案）**通行的做法**是什么？
3. 向渲染器提供**「实时流 + 静音填充」**是否正确？还是应该改为
   「不做静音填充，让 HTTP 请求挂住不返回」？哪种更能让这类音箱保持播放态？
4. 一个 DLNA 渲染器在收到长时间静音后**自行转入 STOPPED**，通常意味着什么？
   是正常行为还是我们违反了它的预期（比如 `Content-Length`、
   `contentFeatures.dlna.org`、DIDL 元数据不符）？
5. `supported_mime = None`（sink 为 `http-get:*:*:*`）时，
   **应当如何选择流格式与 DLNA 头**？我们目前硬发 `audio/wav` + `audio/L16` 回退是否合理？
6. 有没有办法**在不依赖 `pfls`/`pdis` 事件的前提下**可靠识别 seek？
   （真机日志显示 AirPlay 2 拖动进度条时**既不发 `pfls` 也不发 `pdis`**，
   只有 PCM 从新位置继续送来，外加迟到的 `pend`/`pbeg`/`prsm`。）

---

## 九、原始日志获取方式

完整日志在 NAS 上：`/vol1/@appdata/air2dlna/bridge.log`（约 2.3 MB，纯文本）。
关键检索词：

```
换代 / SetAVTransportURI / seek 后重锚 / 渲染器开始拉流 / 渲染器拉流结束
连续输出 / 真实 PCM 恢复 / 静音填充 / RECOVERING / 静音超时
Renderer Profile 选择 / 沿用当前 DLNA 会话 / 重新宣告
AirPlay 事件 / 过渡态 / 预滚动等待不足 / 收敛过程异常
```

代码仓库：https://github.com/youyoudezhuzhu/Air2DLNA
（分支 `feat/virtual-player`；核心文件
`app/server/air2dlna/virtual_player.py`、`dlna_output.py`、`stream.py`、
`renderer_profile.py`）
