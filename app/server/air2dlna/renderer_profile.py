"""DLNA Renderer Profile：把「设备差异」集中成数据，而不是散落在状态机里。

对应 ARCHITECTURE_V2 第 21 节：

* Virtual Player 核心必须保持设备无关；
* 不同 DLNA Renderer 的暂停 / 恢复 / 重连 / 缓冲行为差异，用 Profile 描述；
* ``xiaomi_s12`` 是**已实测**的专门 Profile，``generic`` 是保守兜底；
* Profile 可按渲染器身份（name / model / manufacturer / UDN）自动选择，
  也可以通过配置项 ``renderer_profile`` 强制覆盖。

实测证据（小爱音箱 S12，来自真机 ``bridge.log`` 的 transport state 统计）::

    renderer=STOPPED          3724 次
    renderer=PLAYING            60 次
    renderer=PAUSED_PLAYBACK     2 次   ← 几乎从不出现

    23:06:59 INFO [state] AirPlay 暂停：向渲染器发送 Pause（沿用当前 DLNA 会话 gen=1）
    23:08:09 INFO [state] 诊断: state=PAUSED renderer=STOPPED ... http_clients=1

也就是说：对它调用 ``AVTransport#Pause`` 之后，它**不会停留在 PAUSED_PLAYBACK**，
而是自行进入 ``STOPPED``，并连带丢弃 HTTP 拉流。因此：

* ``supports_pause = False``（不是「不认 Pause 动作」，而是「不会停在暂停态」）
* ``pause_keeps_uri = False`` / ``pause_keeps_http = False``
* ``resume_requires_reannounce = True``（旧 URI 上的裸 ``Play`` 无效，必须重新宣告）
* ``reconnect_behavior = "none"``（已验证不会自行重连 HTTP）

这些差异只表达在 Profile 里；Virtual Player 通过 Profile 决策，绝不把 "S12"
写死在核心逻辑中。``generic`` 保持规格的保守假设：先尝试真正的 Pause 并保留
URI，失败后再退化。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

#: Profile 名称
GENERIC = "generic"
XIAOMI_S12 = "xiaomi_s12"

#: ``reconnect_behavior`` 取值
RECONNECT_AUTO = "auto"        # 设备会自己重建 HTTP 拉流
RECONNECT_NONE = "none"        # 已验证不会自行重连（需要服务端重新宣告）
RECONNECT_UNKNOWN = "unknown"  # 尚未验证

#: ``buffer_behavior`` 取值
BUFFER_TARGET = "target"       # 按 target_buffer_ms 缓冲后开始播放
BUFFER_GREEDY = "greedy"       # 一有数据就尽量开始（对缓冲敏感的设备）


@dataclass(frozen=True)
class RendererProfile:
    """单个 DLNA Renderer 的行为描述。

    字段语义（ARCHITECTURE_V2 第 21 节）：

    ``persistent_stream``
        该设备是否把「当前媒体资源」视为跨暂停/恢复长期有效的资源。
    ``supports_pause``
        设备能否真正**停留在** PAUSED_PLAYBACK（不是「是否接受 Pause 动作」）。
    ``pause_keeps_uri``
        暂停后渲染器是否仍认为当前 URI 有效（恢复时无需 SetAVTransportURI）。
    ``pause_keeps_http``
        暂停后 HTTP 拉流是否保持（否则恢复必须重新建立音频管线）。
    ``supports_range``
        设备是否会真正使用 HTTP Range（本项目的实时流一律按不可寻址处理，
        该字段只用于诊断与能力展示，**绝不**用来把 Range 解释成媒体 Seek）。
    ``supports_seek``
        设备是否支持 AVTransport#Seek（本项目不用它实现媒体 Seek；
        用户请求的 Seek 一律反向作用到 AirPlay，见第 16 节）。
    ``reconnect_behavior``
        设备断开后会否自行重连 HTTP（auto / none / unknown）。
    ``buffer_behavior``
        输出缓冲策略（target / greedy）。
    ``resume_requires_reannounce``
        恢复播放是否必须重新 SetAVTransportURI + Play（设备已 STOPPED）。
    ``minimum_buffer_ms`` / ``target_buffer_ms`` / ``maximum_buffer_ms``
        输出层缓冲水位（第 10 节）。默认 target ≈ 1000ms，不强制精确 1 秒。
    """

    name: str = GENERIC
    persistent_stream: bool = True
    supports_pause: bool = True
    pause_keeps_uri: bool = True
    pause_keeps_http: bool = True
    supports_range: bool = False
    supports_seek: bool = False
    reconnect_behavior: str = RECONNECT_UNKNOWN
    buffer_behavior: str = BUFFER_TARGET
    resume_requires_reannounce: bool = False
    minimum_buffer_ms: int = 400
    target_buffer_ms: int = 1000
    maximum_buffer_ms: int = 1800
    notes: str = ""

    def with_buffer(self, minimum: Optional[int] = None, target: Optional[int] = None,
                    maximum: Optional[int] = None) -> "RendererProfile":
        """返回覆盖了缓冲水位的新 Profile（配置项可覆盖设备默认值）。"""
        return replace(
            self,
            minimum_buffer_ms=self.minimum_buffer_ms if minimum is None else int(minimum),
            target_buffer_ms=self.target_buffer_ms if target is None else int(target),
            maximum_buffer_ms=self.maximum_buffer_ms if maximum is None else int(maximum),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "persistent_stream": self.persistent_stream,
            "supports_pause": self.supports_pause,
            "pause_keeps_uri": self.pause_keeps_uri,
            "pause_keeps_http": self.pause_keeps_http,
            "supports_range": self.supports_range,
            "supports_seek": self.supports_seek,
            "reconnect_behavior": self.reconnect_behavior,
            "buffer_behavior": self.buffer_behavior,
            "resume_requires_reannounce": self.resume_requires_reannounce,
            "minimum_buffer_ms": self.minimum_buffer_ms,
            "target_buffer_ms": self.target_buffer_ms,
            "maximum_buffer_ms": self.maximum_buffer_ms,
            "notes": self.notes,
        }


#: 通用兜底：保持规格第 11 / 12 节的保守假设 —— 先尝试真正的 Pause 并保留 URI。
GENERIC_PROFILE = RendererProfile(
    name=GENERIC,
    persistent_stream=True,
    supports_pause=True,
    pause_keeps_uri=True,
    pause_keeps_http=True,
    supports_range=False,
    supports_seek=False,
    reconnect_behavior=RECONNECT_UNKNOWN,
    buffer_behavior=BUFFER_TARGET,
    resume_requires_reannounce=False,
    notes="保守兜底：优先使用真正的 AVTransport#Pause，保留 URI/位置；失败再退化重建。",
)

#: 小爱音箱 S12：字段取值全部来自真机日志实测（见模块 docstring）。
XIAOMI_S12_PROFILE = RendererProfile(
    name=XIAOMI_S12,
    persistent_stream=False,
    supports_pause=False,
    pause_keeps_uri=False,
    pause_keeps_http=False,
    supports_range=False,
    supports_seek=False,
    reconnect_behavior=RECONNECT_NONE,
    buffer_behavior=BUFFER_GREEDY,
    resume_requires_reannounce=True,
    notes=("实测：Pause 后自行转入 STOPPED 并断开 HTTP（3724 次 STOPPED vs 2 次 "
           "PAUSED_PLAYBACK）。恢复需要重新宣告 URI；暂停期间用输出层静音保持连续。"),
)

#: 已注册的 Profile（配置项 ``renderer_profile`` 可用这些名字强制指定）。
PROFILES: dict[str, RendererProfile] = {
    GENERIC: GENERIC_PROFILE,
    XIAOMI_S12: XIAOMI_S12_PROFILE,
}

#: 身份匹配用的关键字（model / name 中的小写子串）
_S12_HINTS = ("xiaomi s12", "小爱音箱 s12", "s12")


def profile_for(model: str = "", name: str = "", manufacturer: str = "",
                udn: str = "", override: str = "",
                config: Any = None) -> RendererProfile:
    """按渲染器身份选择 Profile。

    ``override`` 非空且命中 :data:`PROFILES` 时直接使用（配置项可覆盖）。
    ``config`` 可选：用于把 ``minimum_buffer_ms`` / ``target_buffer_ms`` /
    ``maximum_buffer_ms`` 三个配置项覆盖到选中的 Profile 上。
    """
    chosen: Optional[RendererProfile] = None
    key = (override or "").strip().lower()
    if key and key in PROFILES:
        chosen = PROFILES[key]
    elif key:
        # 覆盖名未知：绝不猜测，退回 generic 并由调用方记录日志
        chosen = None
    if chosen is None:
        haystack = " ".join(
            part.lower() for part in (model, name, manufacturer, udn) if part
        )
        chosen = XIAOMI_S12_PROFILE if _looks_like_s12(haystack, manufacturer) else GENERIC_PROFILE
    if config is not None:
        chosen = chosen.with_buffer(
            minimum=_config_int(config, "minimum_buffer_ms"),
            target=_config_int(config, "target_buffer_ms"),
            maximum=_config_int(config, "maximum_buffer_ms"),
        )
    return chosen


def _looks_like_s12(haystack: str, manufacturer: str) -> bool:
    """是否是已实测的小爱音箱 S12。

    只按身份特征匹配（型号/名称中的 ``s12``），不做「小米 = S12」这种过度推断。
    """
    for hint in _S12_HINTS:
        if hint in haystack:
            return True
    # 显式厂商 + 型号 S12 的组合也接受
    maker = (manufacturer or "").lower()
    return "xiaomi" in maker and "s12" in haystack


def _config_int(config: Any, key: str) -> Optional[int]:
    try:
        value = config.get(key)
    except Exception:  # noqa: BLE001 - Profile 选择绝不能因配置异常而失败
        return None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def select_profile(record: Any, override: str = "", config: Any = None) -> RendererProfile:
    """从渲染器记录选择 Profile（``record`` 可为 None）。"""
    if record is None:
        base = PROFILES.get((override or "").strip().lower(), GENERIC_PROFILE)
    else:
        base = profile_for(
            model=getattr(record, "model", "") or "",
            name=getattr(record, "name", "") or "",
            manufacturer=getattr(record, "manufacturer", "") or "",
            udn=getattr(record, "udn", "") or "",
            override=override,
        )
    if config is not None:
        base = base.with_buffer(
            minimum=_config_int(config, "minimum_buffer_ms"),
            target=_config_int(config, "target_buffer_ms"),
            maximum=_config_int(config, "maximum_buffer_ms"),
        )
    return base
