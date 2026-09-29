#!/usr/bin/env python3
"""Air2DLNA主进程。

用法（由 ``cmd/main`` 调用，也可独立运行便于开发调试）::

    bridge.py --config-dir DIR --var-dir DIR --ui-dir DIR --bin-dir DIR

环境变量（飞牛在生命周期脚本中提供）优先：

* ``TRIM_PKGETC`` / ``TRIM_PKGVAR`` / ``TRIM_APPDEST`` / ``TRIM_SERVICE_PORT``
* ``TRIM_APPVER``

进程内职责：

1. 准备 FIFO 与 shairport-sync 配置；
2. 监管 shairport-sync（异常退出自动重启）；
3. 读音频 FIFO → PCM 环形缓冲；
4. 读元数据 FIFO → 播放状态机；
5. 发现并控制 DLNA 渲染器；
6. 提供 Web UI / REST API / 实时 PCM 流。
"""

from __future__ import annotations

import argparse
import logging
import os
import select
import signal
import sys
import threading
import time
from typing import Optional

# 允许以脚本方式直接运行（开发调试）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from air2dlna import logging_setup, netif, renderer, ssdp, state, stream, supervisor, upnp, webui  # noqa: E402
from air2dlna.config import Config  # noqa: E402
from air2dlna.metadata import MetadataPipeReader  # noqa: E402
from air2dlna.ringbuffer import PcmRingBuffer  # noqa: E402
from air2dlna.timeline import AudioTimeline  # noqa: E402

log = logging.getLogger("bridge")

DEFAULT_VERSION = "1.0.0"
# 飞牛统一网关为应用分配的路径前缀（与 app/ui/config 的 gatewayPrefix 保持一致）
DEFAULT_GATEWAY_PREFIX = "/app/air2dlna"


class AudioPipeReader(threading.Thread):
    """把 shairport-sync 写出的原始 PCM 排空进环形缓冲。"""

    def __init__(self, path: str, sink, poll_interval: float = 0.1) -> None:
        super().__init__(name="audio-reader", daemon=True)
        self.path = path
        self.sink = sink
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._fd: Optional[int] = None
        self.bytes_read = 0
        self.drained_bytes = 0

    def stop(self) -> None:
        self._stop_event.set()

    def _open(self) -> bool:
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
            log.info("音频管道已打开: %s", self.path)
            return True
        except OSError as exc:
            log.warning("打开音频管道失败（%s），1 秒后重试: %s", exc, self.path)
            return False

    def drain(self) -> int:
        """丢弃管道中残留的数据（seek 时清除 seek 前的尾巴）。"""
        if self._fd is None:
            return 0
        discarded = 0
        while True:
            try:
                chunk = os.read(self._fd, 65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if not chunk:
                break
            discarded += len(chunk)
        if discarded:
            self.drained_bytes += discarded
            log.info("seek：丢弃管道中残留的 %.0f ms 音频",
                     discarded / float(getattr(self.sink, "byte_rate", 176400)))
        return discarded

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self._fd is None:
                if not self._open():
                    self._stop_event.wait(1.0)
                    continue
            try:
                ready, _, _ = select.select([self._fd], [], [], self.poll_interval)
            except (OSError, ValueError):
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                continue
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                log.warning("读取音频管道出错（%s），重新打开", exc)
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
                continue
            if not chunk:
                self._stop_event.wait(0.02)
                continue
            self.bytes_read += len(chunk)
            try:
                self.sink.append(chunk)
            except Exception:  # noqa: BLE001
                log.exception("写入环形缓冲失败")
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class Bridge:
    """应用主控。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config_dir = args.config_dir
        self.var_dir = args.var_dir
        self.ui_dir = args.ui_dir
        self.bin_dir = args.bin_dir
        self.version = args.version
        # 飞牛统一网关：桌面/应用中心通过 Unix 套接字 + 路径前缀访问 Web UI
        self.gateway_socket = args.gateway_socket
        self.gateway_prefix = args.gateway_prefix

        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.var_dir, exist_ok=True)

        self.log_path = os.path.join(self.var_dir, "bridge.log")
        self.audio_fifo = os.path.join(self.var_dir, "audio.fifo")
        self.metadata_fifo = os.path.join(self.var_dir, "metadata.fifo")
        self.shairport_conf = os.path.join(self.var_dir, "shairport-sync.conf")
        self.shairport_log = os.path.join(self.var_dir, "shairport-sync.log")

        self._stop_event = threading.Event()
        self._shairport: Optional[supervisor.ProcessSupervisor] = None
        self._audio_reader: Optional[AudioPipeReader] = None
        self._metadata_reader: Optional[MetadataPipeReader] = None
        self._web: Optional[webui.WebServer] = None
        self._registry: Optional[renderer.RendererRegistry] = None
        self._controller: Optional[state.BridgeController] = None
        self._ring: Optional[PcmRingBuffer] = None
        self._streams: Optional[stream.StreamManager] = None
        self._started_at = time.monotonic()
        self.manage_shairport = not getattr(args, "no_shairport", False)

    # ------------------------------------------------------------------ 启动
    def setup(self) -> None:
        self.config = Config(os.path.join(self.config_dir, "config.json"),
                             log=lambda msg: logging.getLogger("config").info(msg))

        logging_setup.setup_logging(self.log_path, self.config.get("log_level"))
        logging_setup.set_level(self.config.get("log_level"))
        log.info("=" * 72)
        log.info("Air2DLNA 启动中 (version=%s)", self.version)
        log.info("配置目录=%s 运行目录=%s", self.config_dir, self.var_dir)
        log.info("网络接口候选=%s", netif.select_lan_addresses())

        # 1) 命名管道与 shairport-sync 配置
        supervisor.ensure_fifo(self.audio_fifo)
        supervisor.ensure_fifo(self.metadata_fifo)

        # 2) 缓冲 / 时间线 / 流
        sample_rate = int(self.config.get("sample_rate"))
        channels = int(self.config.get("channels"))
        buffer_bytes = int(self.config.get("buffer_seconds")) * sample_rate * channels * 2
        self._ring = PcmRingBuffer(buffer_bytes, sample_rate=sample_rate, channels=channels)
        self.timeline = AudioTimeline(sample_rate=sample_rate)
        self.timeline.set_renderer_latency_ms(float(self.config.get("av_offset_ms")))
        self._streams = stream.StreamManager(self._ring, sample_rate=sample_rate,
                                             channels=channels)

        # 3) 渲染器注册表
        self._registry = renderer.RendererRegistry(
            rediscover_seconds=int(self.config.get("rediscover_seconds"))
        )
        self._registry.restore_selection(
            self.config.get("selected_renderer_udn"),
            self.config.get("selected_renderer_name"),
            self.config.get("selected_renderer_ip"),
        )

        # 4) 控制器
        self._controller = state.BridgeController(
            self.config, self._registry, self._ring, self.timeline, self._streams
        )

        # 5) shairport-sync
        self._write_shairport_config()
        self.shairport_pid_file = os.path.join(self.var_dir, "shairport-sync.pid")
        self._shairport = supervisor.ProcessSupervisor(
            name="shairport-sync",
            argv=[os.path.join(self.bin_dir, "shairport-sync"), "-c", self.shairport_conf],
            log_path=self.shairport_log,
            env=self._child_env(),
            pid_file=self.shairport_pid_file,
        )

        # 6) Web UI / REST API
        ctx = webui.AppContext(
            config=self.config,
            registry=self._registry,
            controller=self._controller,
            streams=self._streams,
            log_path=self.log_path,
            version=self.version,
            ui_dir=self.ui_dir,
            started_at=self._started_at,
            gateway_prefix=self.gateway_prefix,
        )
        ctx.airplay_supervisor = self._shairport
        self._web = webui.WebServer("0.0.0.0", int(self.config.get("http_port")), ctx,
                                    socket_path=self.gateway_socket)

        # 配置热更新
        self.config.add_change_listener(self._on_config_change)
        # seek 时先丢弃管道残留，再换代
        self._controller.set_pre_flush_hook(self._on_pre_flush)

    def _child_env(self) -> dict:
        env = os.environ.copy()
        # 让 shairport-sync 找到随包附带的 .so
        library_dir = os.path.join(os.path.dirname(self.bin_dir), "lib")
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{library_dir}:{existing}" if existing else library_dir
        return env

    def _write_shairport_config(self) -> None:
        log_level = str(self.config.get("log_level")).lower()
        verbosity = {"debug": 3, "info": 1, "warn": 0, "error": 0}.get(log_level, 1)
        text = supervisor.render_shairport_config(
            airplay_name=self.config.get("airplay_name"),
            rtsp_port=int(self.config.get("rtsp_port")),
            audio_fifo=self.audio_fifo,
            metadata_fifo=self.metadata_fifo,
            sample_rate=int(self.config.get("sample_rate")),
            channels=int(self.config.get("channels")),
            log_verbosity=verbosity,
        )
        with open(self.shairport_conf, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.chmod(self.shairport_conf, 0o644)
        except OSError:
            pass
        log.info("已生成 shairport-sync 配置: %s (name=%r, port=%s)",
                 self.shairport_conf, self.config.get("airplay_name"),
                 self.config.get("rtsp_port"))

    def _on_pre_flush(self) -> None:
        if self._audio_reader is not None:
            self._audio_reader.drain()

    def _on_config_change(self, key: str, value) -> None:
        log.info("配置项变更: %s = %r", key, value)
        if key in ("airplay_name", "rtsp_port", "log_level"):
            # 需要重写配置并重启接收器（改名只有重启才生效）
            try:
                self._write_shairport_config()
                if self._shairport is not None:
                    self._shairport.stop()
                    self._shairport.start()
                    self._shairport.start_watchdog()
                if key == "log_level":
                    logging_setup.set_level(str(value))
            except Exception:  # noqa: BLE001
                log.exception("应用配置变更失败")
        elif key == "av_offset_ms":
            self.timeline.set_renderer_latency_ms(float(value))
        elif key == "selected_renderer_udn":
            # 通过 API 选择时控制器已处理，这里只记录
            pass

    # ------------------------------------------------------------------ 运行
    def run(self) -> int:
        assert self._ring and self._streams and self._registry and self._controller

        # 音频读取线程
        self._audio_reader = AudioPipeReader(self.audio_fifo, self._ring)
        self._audio_reader.start()

        # 元数据读取线程
        self._metadata_reader = MetadataPipeReader(
            self.metadata_fifo, self._controller.on_metadata_item
        )
        self._metadata_reader.start()

        self._controller.start()

        # **先启动 HTTP 服务**：飞牛的应用启动健康检查要求服务端口在超时前就绪。
        # 早期实现把 SSDP 发现（约 10 秒）放在 web.start() 之前，导致端口 11 秒后才
        # 打开，应用中心会判定启动失败（错误码 10500）。发现必须完全放到后台。
        assert self._web is not None
        self._web.start()
        log.info("Web UI 已就绪，设备发现转入后台线程")

        # 设备发现全部在后台线程完成（含首轮扫描），不阻塞启动
        self._registry.start_background()

        # 启动 shairport-sync
        assert self._shairport is not None
        if not self.manage_shairport:
            log.info("已跳过 shairport-sync 启动（--no-shairport，测试模式）")
        elif not self._shairport.start():
            log.error(
                "shairport-sync 启动失败。AirPlay 2 接收服务不可用，"
                "请检查 %s 与端口 %s 是否被占用。",
                self.shairport_log, self.config.get("rtsp_port"),
            )
        else:
            log.info("AirPlay service started (name=%r, rtsp_port=%s)",
                     self.config.get("airplay_name"), self.config.get("rtsp_port"))
            log.info("mDNS service registered (_airplay._tcp via avahi)")
        if self.manage_shairport:
            self._shairport.start_watchdog()

        log.info("Air2DLNA已就绪")

        # 主循环等待退出信号
        while not self._stop_event.wait(0.5):
            pass
        return 0

    def shutdown(self) -> None:
        log.info("正在停止 Air2DLNA ...")
        self._stop_event.set()
        for component in (
            self._web.stop if self._web else None,
            self._controller.stop if self._controller else None,
            self._registry.stop if self._registry else None,
            self._metadata_reader.stop if self._metadata_reader else None,
            self._audio_reader.stop if self._audio_reader else None,
        ):
            if component is not None:
                try:
                    component()
                except Exception:  # noqa: BLE001
                    log.exception("停止组件失败")
        if self._shairport is not None:
            self._shairport.stop_watchdog()
            self._shairport.stop()
        if self._ring is not None:
            self._ring.close()
        log.info("已停止")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    appdest = _env("TRIM_APPDEST", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    parser = argparse.ArgumentParser(description="Air2DLNA服务")
    parser.add_argument("--config-dir", default=_env("TRIM_PKGETC", "/tmp/air2dlna/etc"))
    parser.add_argument("--var-dir", default=_env("TRIM_PKGVAR", "/tmp/air2dlna/var"))
    parser.add_argument("--ui-dir", default=os.path.join(appdest, "ui"))
    parser.add_argument("--bin-dir", default=os.path.join(appdest, "server", "bin"))
    parser.add_argument("--version", default=_env("TRIM_APPVER", DEFAULT_VERSION))
    parser.add_argument(
        "--gateway-socket", default=_env("GATEWAY_SOCKET", ""),
        help="飞牛统一网关套接字路径（默认取 GATEWAY_SOCKET 环境变量，留空则不启用）",
    )
    parser.add_argument(
        "--gateway-prefix", default=_env("GATEWAY_PREFIX", DEFAULT_GATEWAY_PREFIX),
        help="飞牛统一网关路径前缀，例如 /app/air2dlna",
    )
    parser.add_argument(
        "--no-shairport", action="store_true",
        help="不启动 shairport-sync（供集成测试使用，避免与已安装实例抢占 7000 端口）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    bridge = Bridge(args)
    bridge.setup()

    def _handle_signal(signum, _frame) -> None:
        log.info("收到信号 %s，准备退出", signum)
        bridge._stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGHUP, _handle_signal)
    except (AttributeError, ValueError):
        pass

    try:
        return bridge.run()
    except Exception:  # noqa: BLE001
        log.exception("桥接进程异常退出")
        return 1
    finally:
        bridge.shutdown()


if __name__ == "__main__":
    sys.exit(main())
