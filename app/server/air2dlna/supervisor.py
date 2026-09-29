"""shairport-sync 配置生成与子进程监管。

对应 TECHNICAL_DESIGN 第 14、15、19 节。

进程分工（最小权限）：

* ``nqptp`` 需要独占 UDP 319/320（特权端口），由 ``cmd/main`` 以 root 启动并看护；
* ``shairport-sync`` 与 ``bridge.py`` 都以**应用用户**运行；
  本模块负责监管 ``shairport-sync``，在它异常退出时按退避策略重启。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from typing import Optional

log = logging.getLogger("supervisor")


def escape_config_string(value: str) -> str:
    """转义 libconfig 字符串，避免配置注入。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_shairport_config(airplay_name: str, rtsp_port: int, audio_fifo: str,
                            metadata_fifo: str, sample_rate: int = 44100,
                            channels: int = 2, backend_buffer_seconds: float = 1.0,
                            progress_interval: float = 0.25,
                            log_verbosity: int = 1) -> str:
    """生成 shairport-sync 配置文本。

    关键点：

    * ``output_backend = "pipe"`` —— 音频以原始 PCM 写入命名管道；
    * ``output_format = "S16_LE"`` / ``output_rate`` / ``output_channels`` ——
      统一为 DLNA 兼容度最高的 LPCM 格式（见 TECHNICAL_DESIGN 第 5 节）；
    * ``mdns_backend = "avahi"`` —— **必须是 avahi**：AirPlay 2 依赖 ``_airplay._tcp``
      服务记录（HomeKit 配对、buffered audio 与 features 位掩码），而 shairport-sync
      只有 avahi 后端会同时注册 ``_raop._tcp`` 与 ``_airplay._tcp``；``tinysvcmdns``
      后端只注册 ``_raop._tcp``，会让 Apple 设备把我们当成 AirPlay 1（RAOP）设备。
      飞牛 OS 自带并运行 avahi-daemon，应用用户亦可通过 D-Bus 发布服务。
    * ``metadata.progress_interval`` —— 周期输出 ``phbt``（RTP 时间戳 + 应播放时刻），
      这是桥接器精确推算播放位置的关键（第 9 节）。
    """
    name = escape_config_string(airplay_name)
    audio = escape_config_string(audio_fifo)
    metadata = escape_config_string(metadata_fifo)
    return f"""// 由 Air2DLNA 自动生成，请勿手工修改（修改会在下次启动时被覆盖）
general =
{{
    name = "{name}";
    port = {int(rtsp_port)};
    output_backend = "pipe";
    mdns_backend = "avahi";
    service_type = "auto";
    playback_mode = "stereo";
    // 让输出缓冲略大于默认值，给桥接与 DLNA 拉流留出余量
    audio_backend_buffer_desired_length_in_seconds = {backend_buffer_seconds};
    audio_decoded_buffer_desired_length_in_seconds = 2.0;
    // AirPlay 音量保持原样上报，桥接器负责映射到 DLNA 音量
    ignore_volume_control = "no";
    volume_range_db = 30;
    drift_tolerance_in_seconds = 0.002;
    resync_threshold_in_seconds = 0.050;
}};

pipe =
{{
    name = "{audio}";
    output_rate = {int(sample_rate)};
    output_format = "S16_LE";
    output_channels = {int(channels)};
}};

metadata =
{{
    enabled = "yes";
    include_cover_art = "yes";
    pipe_name = "{metadata}";
    pipe_timeout = 5000;
    // 周期发送 phbt（RTP 帧号 / 应播放的单调时钟纳秒）
    progress_interval = {progress_interval};
}};

sessioncontrol =
{{
    active_state_timeout = 10.0;
    session_timeout = 30;
    allow_session_interruption = "yes";
    wait_for_completion = "no";
}};

diagnostics =
{{
    log_verbosity = {int(log_verbosity)};
    statistics = "no";
    log_show_file_and_line = "no";
    log_show_time_since_startup = "yes";
    log_show_time_since_last_message = "no";
}};
"""


def ensure_fifo(path: str, mode: int = 0o666) -> None:
    """确保命名管道存在且类型正确。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        if not os.path.stat.S_ISFIFO(os.stat(path).st_mode):
            log.warning("路径已存在且不是 FIFO，删除后重建: %s", path)
            os.unlink(path)
        else:
            os.chmod(path, mode)
            return
    os.mkfifo(path, mode)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    log.info("已创建命名管道: %s", path)


def remove_fifo(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def _write_pid_file(path: str, pid: int) -> None:
    """记录子进程 PID。生命周期脚本据此**精确**终止进程，避免 pkill -f 误杀。"""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="ascii") as handle:
            handle.write(f"{pid}\n")
    except OSError as exc:
        log.warning("写入 PID 文件失败 %s: %s", path, exc)


def _remove_pid_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


#: 单个日志文件的上限与超限后保留的尾部长度（默认 5MB / 1MB）
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_KEEP_BYTES = 1 * 1024 * 1024


def trim_log_file(path: str, max_bytes: int = LOG_MAX_BYTES,
                  keep_bytes: int = LOG_KEEP_BYTES) -> int:
    """文件超过 ``max_bytes`` 时只保留尾部 ``keep_bytes``，返回释放的字节数。

    做法是**原地重写**（同一 inode 上 truncate + write），不是 rename —— 因为写入方
    （子进程 stdout/stderr、shell 的 ``>>``）以 O_APPEND 持有该文件描述符；rename 后
    它们会继续写进已改名的旧 inode，新文件永远收不到内容。原地重写则完全无感。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    if size <= max_bytes:
        return 0
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, size - keep_bytes))
            tail = handle.read()
        # 丢掉可能被截断的首行，避免日志里出现半行
        newline = tail.find(b"\n")
        if newline != -1:
            tail = tail[newline + 1:]
        marker = (f"[logrotate] {time.strftime('%Y-%m-%d %H:%M:%S')} 超过 "
                  f"{max_bytes // (1024 * 1024)}MB，已保留尾部 "
                  f"{keep_bytes // 1024}KB\n").encode("utf-8")
        with open(path, "r+b") as handle:
            handle.seek(0)
            handle.write(marker)
            handle.write(tail)
            handle.truncate()
    except OSError:
        return 0
    try:
        new_size = os.path.getsize(path)
    except OSError:
        new_size = 0
    return max(0, size - new_size)


class ProcessSupervisor:
    """监管一个子进程，异常退出时按退避策略重启。"""

    #: 重启退避序列（秒），之后维持最后一个值
    BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)

    #: 日志上限与保留长度（模块级常量的实例副本，便于测试覆盖）
    log_max_bytes = LOG_MAX_BYTES
    log_keep_bytes = LOG_KEEP_BYTES
    #: 每 N 个看护周期（2s/周期）检查一次日志大小
    LOG_CHECK_EVERY = 30

    def __init__(self, name: str, argv: list[str], log_path: str,
                 env: Optional[dict] = None, cwd: Optional[str] = None,
                 enabled: bool = True, pid_file: Optional[str] = None) -> None:
        self.name = name
        self.argv = argv
        self.log_path = log_path
        self.env = env
        self.cwd = cwd
        self.enabled = enabled
        #: 记录子进程 PID，供生命周期脚本在需要时**精确**终止（避免 pkill -f 误杀）
        self.pid_file = pid_file
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self.restarts = 0
        self.last_exit_code: Optional[int] = None
        self.last_start_monotonic: float = 0.0
        self._log_ticks = 0

    # ------------------------------------------------------------------ 状态
    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def status(self) -> dict:
        return {
            "name": self.name,
            "running": self.running,
            "pid": self._process.pid if self._process is not None else None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "argv": self.argv,
        }

    # ---------------------------------------------------------------- 启停
    def _spawn(self) -> bool:
        if not os.path.exists(self.argv[0]):
            log.error("%s 可执行文件不存在: %s", self.name, self.argv[0])
            return False
        if not os.access(self.argv[0], os.X_OK):
            log.error("%s 不可执行: %s", self.name, self.argv[0])
            return False
        try:
            handle = open(self.log_path, "ab", buffering=0)
        except OSError as exc:
            log.error("%s 无法打开日志文件 %s: %s", self.name, self.log_path, exc)
            return False
        try:
            with self._lock:
                self._process = subprocess.Popen(  # noqa: S603 - 参数数组，无 shell
                    self.argv,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=self.cwd,
                    env=self.env,
                    start_new_session=True,
                )
                self.last_start_monotonic = time.monotonic()
            pid = self._process.pid
            if self.pid_file:
                _write_pid_file(self.pid_file, pid)
            log.info("%s 已启动: pid=%s cmd=%s", self.name, pid, " ".join(self.argv))
            return True
        except OSError as exc:
            log.error("%s 启动失败: %s", self.name, exc)
            return False
        finally:
            handle.close()

    def start(self, wait_seconds: float = 1.5) -> bool:
        if not self.enabled:
            return False
        if self.running:
            return True
        if not self._spawn():
            return False
        time.sleep(wait_seconds)
        if not self.running:
            code = self._process.poll() if self._process is not None else None
            self.last_exit_code = code
            log.error(
                "%s 启动后立即退出 (exit=%s)，请检查日志 %s", self.name, code, self.log_path
            )
            return False
        with self._lock:
            self.restarts = 0
        return True

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            process = self._process
        if process is None:
            if self.pid_file:
                _remove_pid_file(self.pid_file)
            return
        if process.poll() is None:
            log.info("%s 正在停止: pid=%s", self.name, process.pid)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    process.terminate()
                except OSError:
                    pass
            deadline = time.monotonic() + timeout
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if process.poll() is None:
                log.warning("%s 未在 %.0fs 内退出，强制结束", self.name, timeout)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        process.kill()
                    except OSError:
                        pass
        with self._lock:
            self.last_exit_code = process.poll()
            self._process = None
        if self.pid_file:
            _remove_pid_file(self.pid_file)

    # ---------------------------------------------------------------- 看护
    def start_watchdog(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watchdog_loop,
                                        name=f"watchdog-{self.name}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 日志轮转
    def _tick_log_rotation(self) -> None:
        """在看护循环里定期修剪子进程日志，避免无上限增长。

        标准库的 RotatingFileHandler 只管 Python 进程自己的日志；shairport-sync
        这类子进程直接往文件里写，必须由监管方管上限（debug 级别输出量很大）。
        """
        self._log_ticks += 1
        if self._log_ticks % self.LOG_CHECK_EVERY != 0:
            return
        freed = trim_log_file(self.log_path, self.log_max_bytes, self.log_keep_bytes)
        if freed:
            log.info("%s 日志超过上限，已保留尾部并释放 %.1f MB: %s",
                     self.name, freed / (1024 * 1024), self.log_path)

    def _watchdog_loop(self) -> None:
        backoff_index = 0
        while not self._stop_event.is_set():
            self._stop_event.wait(2.0)
            if self._stop_event.is_set() or not self.enabled:
                return
            with self._lock:
                process = self._process
            if process is None:
                continue
            code = process.poll()
            if code is None:
                backoff_index = 0
                self._tick_log_rotation()
                continue
            self.last_exit_code = code
            log.error("%s 异常退出 (exit=%s)，准备重启", self.name, code)
            delay = self.BACKOFF[min(backoff_index, len(self.BACKOFF) - 1)]
            backoff_index += 1
            if self._stop_event.wait(delay):
                return
            if self._spawn():
                with self._lock:
                    self.restarts += 1
                log.info("%s 已重启（第 %d 次）", self.name, self.restarts)

    def stop_watchdog(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
