# libpng: 渐进式读取 `png_process_data_pause` 后 `memcpy` 长度为负

## 概要

| 项 | 值 |
|---|---|
| 库 | libpng（campaign 构建，源码位于 `/root/src/libpng`） |
| 触发 API | `png_process_data()` + 行回调里的 `png_process_data_pause(png_ptr, 1)` |
| 崩溃点 | `png_push_save_buffer()`，`pngpread.c:514` |
| sanitizer | **AddressSanitizer: negative-size-param (size=-1)** |
| 性质 | 整数下溢 → `memcpy` 长度变成约 1.8e19 → 段错误（DoS） |
| 输入 | `input.png`（847 字节，来自 `fuzzer_002` 的 crash artifact） |

## 复现步骤

### 1. 准备容器（挂载 campaign 产物 + 本目录）

```bash
cd /path/to/FuzzAgent
sudo docker run -d --name poc-build --entrypoint sleep \
  -v "/data/.../FuzzAgent/output/7d-libpng-20261117-1339:/root/FuzzAgent/output/libpng:ro" \
  -v "$PWD/poc:/poc:rw" fuzzagent infinity
```

`OUT` 默认 `/root/FuzzAgent/output/libpng`，可用环境变量覆盖。

### 2. 构建

```bash
sudo docker exec -it poc-build bash
cd /poc/libpng
bash build.sh
```

`build.sh` 等价于：

```bash
clang++ -g -O1 -fno-omit-frame-pointer -fsanitize=address,undefined \
  -I$OUT/build/sanitizer/include -I$OUT/build/sanitizer/3rdparty/include \
  poc.cc -o poc \
  -Wl,--start-group $OUT/build/sanitizer/lib/libpng.a \
                    $OUT/build/sanitizer/lib/libpng18.a -Wl,--end-group \
  -lz -lpthread
```

> 必须链接 campaign 里**用 `-fsanitize=address,undefined` 编出来的**静态库，否则 ASan 不会拦截库内的 `memcpy`。

### 3. 运行

```bash
./poc input.png
# 或： ./run.sh   （构建 + 运行 + 保存到 asan-report.txt）
```

退出码 1，stderr 输出 ASan 报告。

## 期望输出（`asan-report.txt`）

```
=================================================================
==144==ERROR: AddressSanitizer: negative-size-param: (size=-1)
    #0 __asan_memcpy
    #1 png_push_save_buffer        /root/src/libpng/pngpread.c:514:7
    #2 png_process_data_pause      /root/src/libpng/pngpread.c:73:10
    #3 png_push_have_row           /root/src/libpng/pngpread.c:1097:7
    #4 ...
    #5 main                        /poc/libpng/poc.cc:52
SUMMARY: AddressSanitizer: negative-size-param ... in __asan_memcpy
==144==ABORTING
```

## 根因

1. 应用使用渐进式（push）读取：`png_process_data()` 分块喂入数据。
2. info 回调启动图像读取并 pause；行回调也调用 `png_process_data_pause(png_ptr, 1)`。
3. 处理 IDAT 时，外层 `png_push_read_IDAT()` 先缓存 `current_buffer_size`，再调用会触发行回调的解压函数。回调中的 pause 把剩余数据保存起来并将 `current_buffer_size` 置零；解压函数返回后，外层仍从该计数中减去先前缓存的长度，导致无符号下溢。
4. 后续 pause 再次进入 `png_push_save_buffer()`，下溢后的巨大计数被传给 `memcpy`。精简版每次喂 1 字节，ASan 报告 **-1**；原 fuzz harness 的分块方式报告 **-69**。

## 可达性 / 影响

- 触发前提：应用用渐进式 API 解码**不可信 PNG**，且行回调里会 pause（流式解码、按需渲染、网络代理都常见）。
- 已验证影响：**DoS**（进程崩溃）；该 PoC 未证明可控内存写或代码执行。
- 不属于远程代码执行。

## 修复建议

`png_push_save_buffer()` 里对 `current_buffer_size` 做非负校验（或改用有符号比较并在 `< 0` 时报 `png_error`），别让负值进入 `memcpy`。
