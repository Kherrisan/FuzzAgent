# FuzzAgent PoCs — 三个 crash 的最小复现

本目录包含 FuzzAgent 在 20 个 benchmark 的 7 天 campaign 中发现的 **3 个独立漏洞**的最小复现代码、构建脚本和 sanitizer 报告。

| 目录 | 目标库 | 问题 | 报告类型 |
|---|---|---|---|
| [`libpng/`](libpng/) | libpng（渐进式读取） | `png_push_save_buffer` 中 `memcpy` 长度为负 | **ASan** `negative-size-param` |
| [`libvpx/`](libvpx/) | libvpx（VP9 SVC 编码） | `pending_frame_sizes[8]` 越界写 | **UBSan + ASan** `heap-buffer-overflow` |
| [`protobuf/`](protobuf/) | protobuf（`GzipInputStream`） | `Skip()` 在 `Next()` 失败后仍调用 `BackUp()` | **Abseil CHECK abort**（见下文说明） |

每个目录都包含：

```
poc.cc / poc.c        复现程序（由对应 harness + 独立 main() 组成）
input.*               触发 crash 的原始 fuzz 输入（从 campaign 产物中取出）
build.sh              编译脚本（链接 campaign 里 ASan/UBSan 插桩过的静态库）
run.sh                一键构建 + 运行 + 保存报告
asan-report.txt       实际运行得到的 sanitizer 报告
```

## 环境

- 镜像：本仓库 `docker/Dockerfile` 构建的 `fuzzagent`（Ubuntu 24.04 + clang 21）
- 编译器：`clang++ 21.1.8`
- 插桩参数：`-fsanitize=address,undefined -fno-omit-frame-pointer -g -O1`
- 被测库：直接复用 campaign 的 `build/sanitizer/lib/*.a`（已用 `-fsanitize=address,undefined` 编译）

## 复现步骤（通用）

1. 启动一个挂载了 campaign 产物和本目录的容器：

```bash
H=$(pwd)   # 仓库根目录
sudo docker run -d --name poc-build --entrypoint sleep \
  -v "/data/.../FuzzAgent/output/7d-libpng-20261117-1339:/root/FuzzAgent/output/libpng:ro" \
  -v "/data/.../FuzzAgent/output/7d-libvpx-20261117-1345:/root/FuzzAgent/output/libvpx:ro" \
  -v "/data/.../FuzzAgent/output/7d-protobuf-20261117-1344:/root/FuzzAgent/output/protobuf:ro" \
  -v "$H/poc:/poc:rw" \
  fuzzagent infinity
```

> 注意：本机 dockerd 的挂载命名空间与 shell 不同，宿主机路径要写成 daemon 视角的 `/data/...`（`benchmark/run-wrapper.sh` 做的就是这件事）。

2. 进入容器逐个复现：

```bash
sudo docker exec -it poc-build bash
cd /poc/libpng   && ./run.sh     # ASan negative-size-param
cd /poc/libvpx   && ./run.sh     # UBSan OOB index + ASan heap-buffer-overflow
cd /poc/protobuf && ./run.sh     # Abseil CHECK 失败并 abort
```

3. 每次运行都会把完整报告写到对应目录的 `asan-report.txt`。

## 三条问题的性质

**libpng** —— 真内存错误，ASan 直接报 `negative-size-param: (size=-69)`；release 构建里这等价于 `memcpy(dst, src, ~1.8e19)`，会立即段错误。影响：DoS。

**libvpx** —— UBSan 先报 `index 8 out of bounds for type 'size_t[8]'`（`vp9_cx_iface.c:1620`），随后 ASan 报 `heap-buffer-overflow`。这是真正的越界写，属于内存破坏类问题。

**protobuf** —— `GzipInputStream::Skip()` 在 `Next()` 返回失败后仍用陈旧的 `size` 调用 `BackUp()`，触发 `ABSL_CHECK_LE(count, max_backup)` → 进程 abort。这是**断言中止（DoS）**，不是 ASan 能捕获的内存访问；`protobuf/README.md` 里有进一步说明和一个去掉 CHECK 的对照实验。

## 免责说明

这些 PoC 由 fuzzer harness 改写而来（harness 本身就是“调用库 API 的最小程序”），因此复现的是**库的 API 层**行为，不等同于端到端应用漏洞。是否需要真实产品受影响的论证，见各目录 README 的“可达性”一节。
