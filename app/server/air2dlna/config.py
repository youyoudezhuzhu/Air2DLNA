"""配置管理：加载、校验、持久化与升级迁移。

配置保存在 ``TRIM_PKGETC/config.json``（飞牛规范：应用配置目录，升级保留）。
本模块不依赖任何第三方库。
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from typing import Any

SCHEMA_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    # 任务书第 5 节：AirPlay 音箱名称，与 DLNA 设备名完全独立
    "airplay_name": "Feiniu AirPlay",
    # 任务书第 7 节：用户选定的 DLNA Renderer（不自动切换）
    "selected_renderer_udn": "",
    "selected_renderer_name": "",
    "selected_renderer_ip": "",
    "log_level": "info",
    # AirPlay RTSP 端口（mDNS 会广播该端口，因此可以用非 7000 的值）
    "rtsp_port": 7000,
    # Web UI / REST API / PCM 流 / GENA 回调 端口
    "http_port": 8788,
    # 推流格式偏好："auto" | "wav" | "l16"
    "output_format": "auto",
    # 重建 DLNA 会话前预滚动的秒数
    "preroll_seconds": 2.0,
    # PCM 环形缓冲容量（秒）
    "buffer_seconds": 120,
    # 音画/进度偏移补偿（毫秒，可正可负）
    "av_offset_ms": 0,
    # 渲染器与内部时间线的漂移阈值（毫秒）
    "drift_threshold_ms": 1500,
    # 暂停恢复时先写入的静音时长（毫秒）：让渲染器一连上新 URI 就有数据可读。
    # 默认 0 = 不预填（保持原行为）。仅用于 A/B 测试「预填数据能否缩短出声时间」。
    "resume_prebuffer_ms": 0,
    # 暂停恢复策略（实验开关，GPT 计划 Phase 1 的目标是 keepalive）：
    #   current   = 稳定后备：DLNA Pause → 恢复时换代 + SetURI + Play（约 2~4 秒）
    #   keepalive = 暂停时不碰渲染器（保持 PLAYING），HTTP 输出层送静音；
    #               恢复时直接切回真实 PCM（目标 <500ms，失败自动回落 current）
    #   prewarm   = 暂停期间提前换代 + SetURI（不 Play），恢复时只补一个 Play
    #   auto      = 先 keepalive，失败/超时自动退化到 current
    "recovery_mode": "keepalive",
    # keepalive 最长保持时间（秒）：超时后回落 current，避免音箱长期空转
    "pause_keepalive_timeout_seconds": 30,
    # GENA 不可用时的位置轮询间隔（秒）
    "metadata_poll_seconds": 3.0,
    # SSDP 后台重扫间隔（秒）
    "rediscover_seconds": 300,
    # 采样率/声道（固定 44.1k/2ch，见 TECHNICAL_DESIGN 第 5 节）
    "sample_rate": 44100,
    "channels": 2,
}

# 允许通过 REST API 修改的键（白名单，避免注入任意字段）
EDITABLE_KEYS = {
    "airplay_name",
    "selected_renderer_udn",
    "selected_renderer_name",
    "selected_renderer_ip",
    "log_level",
    "rtsp_port",
    "http_port",
    "output_format",
    "preroll_seconds",
    "buffer_seconds",
    "av_offset_ms",
    "drift_threshold_ms",
    "resume_prebuffer_ms",
    "recovery_mode",
    "pause_keepalive_timeout_seconds",
    "metadata_poll_seconds",
    "rediscover_seconds",
}

LOG_LEVELS = ("debug", "info", "warn", "error")
OUTPUT_FORMATS = ("auto", "wav", "l16")

# AirPlay 名称会被写入 shairport-sync 配置文件，必须限制字符集合以防注入。
_NAME_ALLOWED = re.compile(r"^[^\x00-\x1f\x7f\"\\]{1,50}$")


class ConfigError(ValueError):
    """配置校验失败。"""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def validate_value(key: str, value: Any) -> Any:
    """校验并归一化单个配置值，失败抛 :class:`ConfigError`。"""
    if key == "airplay_name":
        text = str(value).strip()
        if not text:
            raise ConfigError("AirPlay 音箱名称不能为空")
        if not _NAME_ALLOWED.match(text):
            raise ConfigError('AirPlay 音箱名称不能包含引号、反斜杠、控制字符，且长度不超过 50')
        return text
    if key in ("selected_renderer_udn", "selected_renderer_name", "selected_renderer_ip"):
        return str(value).strip()[:255]
    if key == "log_level":
        text = str(value).strip().lower()
        if text not in LOG_LEVELS:
            raise ConfigError(f"log_level 必须是 {', '.join(LOG_LEVELS)} 之一")
        return text
    if key == "output_format":
        text = str(value).strip().lower()
        if text not in OUTPUT_FORMATS:
            raise ConfigError(f"output_format 必须是 {', '.join(OUTPUT_FORMATS)} 之一")
        return text
    if key in ("rtsp_port", "http_port"):
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} 必须是整数") from None
        if not (1024 <= port <= 65535):
            raise ConfigError(f"{key} 必须在 1024-65535 之间（非 root 不可绑定 <1024）")
        return port
    if key == "preroll_seconds":
        try:
            sec = float(value)
        except (TypeError, ValueError):
            raise ConfigError("preroll_seconds 必须是数字") from None
        if not (0.2 <= sec <= 15.0):
            raise ConfigError("preroll_seconds 必须在 0.2-15.0 之间")
        return round(sec, 2)
    if key == "buffer_seconds":
        try:
            sec = int(value)
        except (TypeError, ValueError):
            raise ConfigError("buffer_seconds 必须是整数") from None
        if not (10 <= sec <= 900):
            raise ConfigError("buffer_seconds 必须在 10-900 之间")
        return sec
    if key == "av_offset_ms":
        try:
            ms = int(value)
        except (TypeError, ValueError):
            raise ConfigError("av_offset_ms 必须是整数") from None
        if not (-10000 <= ms <= 10000):
            raise ConfigError("av_offset_ms 必须在 -10000..10000 之间")
        return ms
    if key == "drift_threshold_ms":
        try:
            ms = int(value)
        except (TypeError, ValueError):
            raise ConfigError("drift_threshold_ms 必须是整数") from None
        if not (100 <= ms <= 30000):
            raise ConfigError("drift_threshold_ms 必须在 100-30000 之间")
        return ms
    if key == "metadata_poll_seconds":
        try:
            sec = float(value)
        except (TypeError, ValueError):
            raise ConfigError("metadata_poll_seconds 必须是数字") from None
        if not (1.0 <= sec <= 60.0):
            raise ConfigError("metadata_poll_seconds 必须在 1.0-60.0 之间")
        return round(sec, 2)
    if key == "rediscover_seconds":
        try:
            sec = int(value)
        except (TypeError, ValueError):
            raise ConfigError("rediscover_seconds 必须是整数") from None
        if not (30 <= sec <= 3600):
            raise ConfigError("rediscover_seconds 必须在 30-3600 之间")
        return sec
    if key == "sample_rate":
        try:
            rate = int(value)
        except (TypeError, ValueError):
            raise ConfigError("sample_rate 必须是整数") from None
        if rate not in (44100, 48000):
            raise ConfigError("sample_rate 只支持 44100 或 48000")
        return rate
    if key == "channels":
        try:
            ch = int(value)
        except (TypeError, ValueError):
            raise ConfigError("channels 必须是整数") from None
        if ch != 2:
            raise ConfigError("第一阶段只支持 2 声道")
        return ch
    raise ConfigError(f"未知配置项: {key}")


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """把任意历史配置迁移到当前 schema。幂等。"""
    cfg = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    for key, value in raw.items():
        if key == "schema_version":
            continue
        if key in DEFAULTS:
            try:
                cfg[key] = validate_value(key, value)
            except ConfigError:
                # 非法历史值退回默认值，绝不让应用因旧配置起不来
                cfg[key] = copy.deepcopy(DEFAULTS[key])
    cfg["schema_version"] = SCHEMA_VERSION
    return cfg


class Config:
    """线程安全的配置容器，写操作原子落盘。"""

    def __init__(self, path: str, log=None) -> None:
        self.path = path
        self._log = log
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._on_change = []  # list[callable[[str, Any], None]]
        self.load()

    # ---------------------------------------------------------------- 通知
    def add_change_listener(self, callback) -> None:
        """注册配置变更回调 ``callback(key, value)``。"""
        self._on_change.append(callback)

    def _notify(self, key: str, value: Any) -> None:
        for callback in self._on_change:
            try:
                callback(key, value)
            except Exception:  # noqa: BLE001 - 监听器异常不能影响配置写入
                if self._log:
                    self._log(f"配置变更监听器异常: key={key}")

    # ---------------------------------------------------------------- 读写
    def load(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            if self._log:
                self._log(f"配置文件不存在，使用默认配置: {self.path}")
        except (OSError, json.JSONDecodeError) as exc:
            if self._log:
                self._log(f"配置文件不可用（{exc}），使用默认配置: {self.path}")
        with self._lock:
            self._data = migrate(raw)
            return copy.deepcopy(self._data)

    def save(self) -> None:
        """原子写入：先写临时文件再 rename，避免掉电损坏配置。"""
        with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def update(self, changes: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        """批量校验并更新配置。任一项非法则整批拒绝（保持旧配置可用）。"""
        if not isinstance(changes, dict):
            raise ConfigError("请求体必须是 JSON 对象")
        unknown = set(changes) - EDITABLE_KEYS
        if unknown:
            raise ConfigError(f"不允许修改的配置项: {', '.join(sorted(unknown))}")
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            normalized[key] = validate_value(key, value)
        with self._lock:
            for key, value in normalized.items():
                self._data[key] = value
        if persist:
            self.save()
        for key, value in normalized.items():
            self._notify(key, value)
        return self.snapshot()
