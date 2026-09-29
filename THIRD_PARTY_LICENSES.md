# 第三方组件与许可证

本仓库**自身的代码**以 [MIT](LICENSE) 授权。

打包出的 `.fpk` 中**还包含**下列第三方程序。它们以**独立进程**或**动态链接的共享库**
形式随包分发，各自遵循其原始许可证。

## 1. 随包分发的程序与库

| 组件 | 版本 | 许可证 | 分发形式 | 上游 |
|---|---|---|---|---|
| shairport-sync | 5.5.1 | MIT（逐文件声明；`nqptp-shm-structures.h` 来自 NQPTP，为 GPL-2.0） | 可执行文件 `server/bin/shairport-sync` | https://github.com/mikebrady/shairport-sync |
| NQPTP | 1.2.8 | **GPL-2.0** | 可执行文件 `server/bin/nqptp`（**独立进程**） | https://github.com/mikebrady/nqptp |
| FFmpeg | n7.1（libavcodec 61.19.100 / libavformat 61.7.100 / libavutil 59.39.100 / libswresample 5.3.100） | **LGPL-2.1+** | **共享库**（`server/lib/libav*.so*`），由 shairport-sync **动态链接** | https://ffmpeg.org / https://github.com/FFmpeg/FFmpeg |
| libconfig | 1.5（libconfig.so.9） | LGPL-2.1 | 共享库 | https://hyperrealm.github.io/libconfig/ |
| libsoxr | 0.1.3（libsoxr.so.0） | LGPL-2.1+ | 共享库 | https://sourceforge.net/projects/soxr/ |
| libplist | 2.2.0（libplist-2.0.so.3） | LGPL-2.1 | 共享库 | https://github.com/libimobiledevice/libplist |
| popt | 1.19（libpopt.so.0） | MIT（expat） | 共享库 | https://github.com/rpm-software-management/popt |
| libsodium | 1.0.18（libsodium.so.23） | ISC | 共享库 | https://libsodium.org |
| libgcrypt | 1.10.1（libgcrypt.so.20） | LGPL-2.1+ | 共享库 | https://gnupg.org/software/libgcrypt/ |

以下库**不随包分发**，直接使用目标系统（飞牛 OS 自带）的版本：
`libc`、`libm`、`libgomp`、`libcrypto`(OpenSSL 3)、`libuuid`、`libgpg-error`、
`libavahi-client.so.3`、`libavahi-common.so.3`。

## 2. 合规说明

### FFmpeg（LGPL-2.1+）—— 采用动态链接

FFmpeg 在本项目中**只用于 AAC 解码**，并且通过

```
--disable-everything --enable-decoder=aac --enable-decoder=aac_fixed \
--enable-decoder=aac_latm --enable-decoder=alac \
--enable-parser=aac --enable-parser=aac_latm
```

裁剪构建，**未启用任何 GPL 组件**（没有 `--enable-gpl` / `--enable-nonfree`），
因此整体为 LGPL-2.1-or-later。

为满足 LGPL 关于「允许用户以修改后的库重新链接」的要求，本项目：

* 以**共享库**方式（`--enable-shared --disable-static`）构建 FFmpeg，
  shairport-sync 通过 `DT_RUNPATH=$ORIGIN/../lib` **动态链接**这些 `.so`；
  用户可用自行修改/替换的 FFmpeg 共享库直接替换 `server/lib/` 下的同名文件；
* 提供**完整的可复现构建脚本**（`scripts/build.sh`），其中包含 FFmpeg 的确切
  版本（`n7.1`）、`configure` 参数与构建步骤。

### NQPTP（GPL-2.0）—— 以独立进程分发

NQPTP 是 AirPlay 2 计时所需的 PTP 时钟守护进程。它与本项目的其它部分**仅通过
POSIX 共享内存 `/dev/shm/nqptp` 通信，属于独立程序的聚合（aggregation），
不构成衍生作品**，因此不会把本仓库的 MIT 代码传染为 GPL。

按 GPL-2.0 的要求，本项目：

* 在包内保留其原始二进制与出处（版本 1.2.8，上游仓库地址见上表）；
* 不对其做任何修改（`scripts/build.sh` 直接构建上游 commit）；
* 对应的完整源代码可从上游仓库获取，或由 `scripts/build.sh` 自动获取并构建；
* 许可证全文见 https://github.com/mikebrady/nqptp/blob/master/LICENSE 。

若你需要以非 GPL 条款分发包含 NQPTP 的整包，请自行联系 NQPTP 作者获取商业许可。

### shairport-sync（MIT）

按文件声明许可证。本项目仅做编译配置（不修改源码），二进制自报版本字符串：
`01078ad-AirPlay2-smi10-OpenSSL-Avahi-stdout-pipe-soxr-metadata`。

## 3. 源码获取

所有第三方组件的对应源代码均可通过以下方式获得：

```bash
# 本项目会自动克隆并构建这些上游仓库
./scripts/build.sh

# 或手动获取
git clone https://github.com/mikebrady/shairport-sync.git   # 5.5.1
git clone https://github.com/mikebrady/nqptp.git            # 1.2.8
git clone --branch n7.1 https://github.com/FFmpeg/FFmpeg.git
```

Debian 包形式的共享库源码可通过 `apt-get source <包名>` 获取，
或在 https://sources.debian.org 查询。
