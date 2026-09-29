#!/bin/bash
# AirPlay2-DLNA-Bridge —— 可复现构建脚本
#
# 从源码构建全部原生组件并打包出 .fpk。**不使用 Docker**。
#
#   ./scripts/build.sh              # 增量构建（已构建的组件会跳过）
#   ./scripts/build.sh --force      # 全部重建
#   ./scripts/build.sh --skip-native  # 只重新打包（组件已就位）
#   ./scripts/build.sh --no-pack      # 只构建原生组件（CI 用，无需 fnpack）
#
# 产物：dist/AirPlay2-DLNA-Bridge-<version>.fpk
#
# 构建依赖（脚本会自动安装）：
#   build-essential autoconf automake libtool pkg-config git xxd plistutil
#   libpopt-dev libconfig-dev libssl-dev libsoxr-dev libplist-dev
#   libsodium-dev libgcrypt20-dev uuid-dev
#
# 注意：**不能** apt 安装 libavahi-client-dev —— 飞牛 OS 自带的 avahi 包版本带
# `+trim-1` 后缀，与 Debian dev 包的精确版本依赖冲突。因此本脚本只「下载并解包」
# 这两个 dev 包到临时目录用于编译，绝不安装或替换系统包。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${A2D_BUILD_DIR:-/tmp/a2d-build}"
DIST="$ROOT/dist"
FFMPEG_PREFIX="$WORK/ffmpeg"
AVAHI_ROOT="$WORK/avahi-dev"
SS_PREFIX="$WORK/ss-prefix"

FORCE=0
SKIP_NATIVE=0
NO_PACK=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --skip-native) SKIP_NATIVE=1 ;;
        --no-pack) NO_PACK=1 ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "$ROOT/manifest" | tr -d '[:space:]')"
[ -n "$VERSION" ] || die "无法从 manifest 读取 version"

mkdir -p "$WORK" "$DIST"

# --------------------------------------------------------------------- 依赖
install_deps() {
    log "安装构建依赖"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq \
        build-essential autoconf automake libtool pkg-config git \
        libpopt-dev libconfig-dev libssl-dev libsoxr-dev libplist-dev \
        libsodium-dev libgcrypt20-dev uuid-dev \
        libplist-utils xxd patchelf >/dev/null
    command -v pkg-config >/dev/null || die "pkg-config 不可用"
    command -v plistutil >/dev/null || die "plistutil 不可用"
    command -v xxd       >/dev/null || die "xxd 不可用"
}

# Avahi 开发文件：下载并解包到临时目录（不安装）
prepare_avahi_dev() {
    if [ -f "$AVAHI_ROOT/usr/lib/x86_64-linux-gnu/pkgconfig/avahi-client.pc" ] && [ "$FORCE" -eq 0 ]; then
        log "Avahi 开发文件已就绪，跳过"
        return
    fi
    log "下载并解包 Avahi 开发文件（不安装，避免与飞牛自带 +trim 版本冲突）"
    rm -rf "$AVAHI_ROOT"; mkdir -p "$AVAHI_ROOT/deb"
    ( cd "$AVAHI_ROOT/deb"
      DEBIAN_FRONTEND=noninteractive apt-get download libavahi-client-dev libavahi-common-dev >/dev/null 2>&1 \
        || die "下载 avahi dev 包失败" )
    for deb in "$AVAHI_ROOT"/deb/*.deb; do
        dpkg-deb -x "$deb" "$AVAHI_ROOT"
    done
    local libdir="$AVAHI_ROOT/usr/lib/x86_64-linux-gnu"
    # .pc 里的路径指向真实 /usr，改为临时根
    sed -i "s|^prefix=/usr$|prefix=$AVAHI_ROOT/usr|; s|^libdir=/usr/lib/x86_64-linux-gnu$|libdir=$libdir|" \
        "$libdir"/pkgconfig/*.pc
    # dev 包只提供 .so 符号链接，指向运行期 .so.3.*；这里指向系统自带的真实库
    local real_client real_common
    real_client="$(readlink -f /lib/x86_64-linux-gnu/libavahi-client.so.3 2>/dev/null || true)"
    real_common="$(readlink -f /lib/x86_64-linux-gnu/libavahi-common.so.3 2>/dev/null || true)"
    [ -n "$real_client" ] && ln -sf "$real_client" "$libdir/libavahi-client.so"
    [ -n "$real_common" ] && ln -sf "$real_common" "$libdir/libavahi-common.so"
    [ -n "$real_client" ] || die "系统缺少 libavahi-client.so.3（飞牛应自带 avahi-daemon）"
}

# 最小化 FFmpeg：只保留 AAC / ALAC 解码器。
# **必须用共享库**：FFmpeg 是 LGPL-2.1+，静态链接会触发 LGPL 的可重链接义务；
# 动态链接 + 随包分发 .so 是最干净的做法。
build_ffmpeg() {
    if [ -f "$FFMPEG_PREFIX/lib/libavcodec.so" ] && [ "$FORCE" -eq 0 ]; then
        log "FFmpeg 共享库已就绪，跳过"
        return
    fi
    log "构建最小化静态 FFmpeg（仅 AAC/ALAC 解码器）"
    rm -rf "$WORK/ffmpeg-src"
    git clone --depth 1 --branch n7.1 https://github.com/FFmpeg/FFmpeg.git "$WORK/ffmpeg-src" \
        >/dev/null 2>&1 || die "克隆 FFmpeg 失败"
    (
      cd "$WORK/ffmpeg-src"
      ./configure --prefix="$FFMPEG_PREFIX" \
        --disable-everything --disable-programs --disable-doc --disable-avdevice \
        --disable-swscale --disable-postproc --disable-avfilter --disable-network \
        --disable-x86asm --enable-shared --disable-static --enable-pic \
        --enable-decoder=aac --enable-decoder=aac_fixed --enable-decoder=aac_latm \
        --enable-decoder=alac --enable-parser=aac --enable-parser=aac_latm \
        > "$WORK/ffmpeg-configure.log" 2>&1 || { tail -20 "$WORK/ffmpeg-configure.log"; die "FFmpeg configure 失败"; }
      make -j"$(nproc)" > "$WORK/ffmpeg-make.log" 2>&1 || { tail -30 "$WORK/ffmpeg-make.log"; die "FFmpeg 编译失败"; }
      make install >> "$WORK/ffmpeg-make.log" 2>&1
    )
}

# NQPTP：AirPlay 2 的 PTP 时钟守护进程
build_nqptp() {
    if [ -x "$WORK/nqptp-src/nqptp" ] && [ "$FORCE" -eq 0 ]; then
        log "nqptp 已构建，跳过"
        return
    fi
    log "构建 NQPTP"
    rm -rf "$WORK/nqptp-src"
    git clone --depth 1 https://github.com/mikebrady/nqptp.git "$WORK/nqptp-src" >/dev/null 2>&1 \
        || die "克隆 nqptp 失败"
    (
      cd "$WORK/nqptp-src"
      autoreconf -fi > "$WORK/nqptp-build.log" 2>&1
      ./configure >> "$WORK/nqptp-build.log" 2>&1
      make -j"$(nproc)" >> "$WORK/nqptp-build.log" 2>&1 || { tail -30 "$WORK/nqptp-build.log"; die "nqptp 编译失败"; }
    )
}

# shairport-sync：AirPlay 2 接收器（必须用 avahi，才能注册 _airplay._tcp）
build_shairport() {
    if [ -x "$WORK/shairport-sync-src/shairport-sync" ] && [ "$FORCE" -eq 0 ]; then
        log "shairport-sync 已构建，跳过"
        return
    fi
    log "构建 shairport-sync（AirPlay 2 + avahi + pipe/stdout + metadata + soxr + ffmpeg）"
    rm -rf "$WORK/shairport-sync-src"
    git clone --depth 1 https://github.com/mikebrady/shairport-sync.git "$WORK/shairport-sync-src" >/dev/null 2>&1 \
        || die "克隆 shairport-sync 失败"
    local avahi_libdir="$AVAHI_ROOT/usr/lib/x86_64-linux-gnu"
    (
      cd "$WORK/shairport-sync-src"
      autoreconf -fi > "$WORK/ss-build.log" 2>&1
      export PKG_CONFIG_PATH="$FFMPEG_PREFIX/lib/pkgconfig:$avahi_libdir/pkgconfig"
      export CFLAGS="-I$FFMPEG_PREFIX/include -I$AVAHI_ROOT/usr/include -O2"
      export LDFLAGS="-L$FFMPEG_PREFIX/lib -L$avahi_libdir -Wl,-rpath,\$ORIGIN/../lib"
      ./configure --prefix="$SS_PREFIX" \
        --with-os=linux --with-airplay-2 \
        --with-pipe --with-stdout \
        --with-metadata --with-metadata-pipe \
        --with-soxr --with-ffmpeg \
        --with-avahi --with-ssl=openssl \
        --without-alsa --without-jack --without-sndio --without-ao --without-soundio \
        --without-pulseaudio --without-pipewire --without-convolution \
        --without-dbus-interface --without-mpris-interface --without-mqtt-client \
        --without-libdaemon --without-dns_sd --without-external-mdns \
        --without-systemd-startup --without-systemv-startup \
        --without-create-user-group --without-configfiles >> "$WORK/ss-build.log" 2>&1 \
        || { tail -25 "$WORK/ss-build.log"; die "shairport-sync configure 失败"; }
      make -j"$(nproc)" >> "$WORK/ss-build.log" 2>&1 || { tail -30 "$WORK/ss-build.log"; die "shairport-sync 编译失败"; }
    )
}

# 把二进制与随包 .so 放进 app/server
stage_native() {
    log "收集原生二进制与运行库"
    local bin="$ROOT/app/server/bin" lib="$ROOT/app/server/lib"
    mkdir -p "$bin" "$lib"

    strip -o "$bin/shairport-sync" "$WORK/shairport-sync-src/shairport-sync"
    strip -o "$bin/nqptp" "$WORK/nqptp-src/nqptp"
    chmod 755 "$bin/shairport-sync" "$bin/nqptp"
    patchelf --set-rpath '$ORIGIN/../lib' "$bin/shairport-sync"
    patchelf --set-rpath '$ORIGIN/../lib' "$bin/nqptp"

    # 随包分发飞牛可能不含有的库；glibc/libcrypto/libgomp/libuuid/libgpg-error
    # 属于基础系统库，一律使用系统自带（也便于随系统获得安全更新）。
    # FFmpeg 共享库（LGPL-2.1+）：必须随包分发，且**动态**链接。
    # 步骤：① 复制带完整版本号的真实文件；② 依据上游的 SONAME 符号链接建立同名链接。
    # 注意：绝不能用推导出的名字去 ln -sf 真实文件（会做出指向自身的软链，把库弄坏——
    # 这正是 CI 上 "libswresample.so.5: cannot open shared object file" 的根因）。
    local ff ff_base link target
    mkdir -p "$lib"
    rm -f "$lib"/libav*.so* "$lib"/libswresample.so*
    for ff in "$FFMPEG_PREFIX"/lib/libavcodec.so.*.*.* "$FFMPEG_PREFIX"/lib/libavformat.so.*.*.* \
              "$FFMPEG_PREFIX"/lib/libavutil.so.*.*.* "$FFMPEG_PREFIX"/lib/libswresample.so.*.*.*; do
        [ -f "$ff" ] || continue
        ff_base="$(basename "$ff")"
        cp -f "$ff" "$lib/$ff_base"
        chmod 644 "$lib/$ff_base"
    done
    for link in "$FFMPEG_PREFIX"/lib/libav*.so.[0-9]* "$FFMPEG_PREFIX"/lib/libswresample.so.[0-9]*; do
        [ -L "$link" ] || continue
        target="$(readlink -f "$link")"
        [ -f "$target" ] || continue
        ln -sf "$(basename "$target")" "$lib/$(basename "$link")"
    done
    # 库文件必须存在且不是坏链
    for link in libavcodec libavformat libavutil libswresample; do
        if ! compgen -G "$lib/$link.so.[0-9]*" >/dev/null; then
            die "随包缺少 $link 的共享库（FFmpeg 未正确安装？）"
        fi
    done

    local bundled=(libconfig.so.9 libsoxr.so.0 libplist-2.0.so.3 libpopt.so.0 libsodium.so.23 libgcrypt.so.20)
    local name src
    for name in "${bundled[@]}"; do
        src="$(readlink -f "/lib/x86_64-linux-gnu/$name" 2>/dev/null || true)"
        if [ -z "$src" ] || [ ! -f "$src" ]; then
            warn "未找到 $name，将由系统提供"
            continue
        fi
        cp -L "$src" "$lib/$name"
        chmod 644 "$lib/$name"
    done

    # 真正的加载校验：RPATH 指向随包 lib，必须能独立启动
    local ver
    if ! ver="$("$bin/shairport-sync" -V 2>&1)"; then
        echo "--- ldd $bin/shairport-sync ---" >&2
        ldd "$bin/shairport-sync" >&2 || true
        echo "--- $lib ---" >&2
        ls -la "$lib" >&2 || true
        die "shairport-sync 无法加载（检查随包共享库与 RPATH）: $ver"
    fi
    log "shairport-sync 能力： $ver"
    if ! "$bin/shairport-sync" -V 2>&1 | grep -q 'AirPlay2'; then
        die "构建出的 shairport-sync 未启用 AirPlay 2"
    fi
    if ! "$bin/shairport-sync" -V 2>&1 | grep -q 'Avahi'; then
        die "shairport-sync 未使用 Avahi —— AirPlay 2 的 _airplay._tcp 将无法注册"
    fi
    # 必须动态链接 FFmpeg（LGPL 合规），而不是静态链进去
    if ! ldd "$bin/shairport-sync" | grep -q 'libavcodec'; then
        die "shairport-sync 未动态链接 FFmpeg 共享库（LGPL 合规要求）"
    fi
    if ! ls "$lib"/libavcodec.so.* >/dev/null 2>&1; then
        die "随包缺少 FFmpeg 共享库"
    fi
}

# 打包
pack() {
    log "清理临时文件并用 fnpack 打包"
    find "$ROOT" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$ROOT" -name '*.pyc' -delete 2>/dev/null || true
    chmod 755 "$ROOT"/cmd/* "$ROOT"/app/server/*.sh "$ROOT"/app/server/*.py
    chmod 755 "$ROOT"/app/server/bin/* 2>/dev/null || true
    chmod -R a+rX "$ROOT/app" "$ROOT/cmd" "$ROOT/config" "$ROOT/wizard" 2>/dev/null || true

    command -v fnpack >/dev/null || die "未找到 fnpack（飞牛官方打包工具）"
    ( cd "$WORK" && rm -f ./*.fpk && fnpack build --directory "$ROOT" >/dev/null )

    local out="$DIST/AirPlay2-DLNA-Bridge-$VERSION.fpk"
    mkdir -p "$DIST"
    mv "$WORK/airplay2dlna.fpk" "$out"
    log "打包完成：$out ($(du -h "$out" | cut -f1))"
    echo "$out"
}

log "AirPlay2-DLNA-Bridge 构建 (version=$VERSION)"
if [ "$SKIP_NATIVE" -eq 0 ]; then
    install_deps
    prepare_avahi_dev
    build_ffmpeg
    build_nqptp
    build_shairport
    stage_native
else
    log "跳过原生组件构建"
fi
if [ "$NO_PACK" -eq 1 ]; then
    log "已跳过 fnpack 打包（--no-pack）"
else
    pack
fi
