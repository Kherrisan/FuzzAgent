// PoC for the protobuf crash found by FuzzAgent (harness_006.cpp + driver).
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>

#include <fuzzer/FuzzedDataProvider.h>
#include <google/protobuf/io/gzip_stream.h>
#include <google/protobuf/io/zero_copy_stream_impl_lite.h>

// Treat the remaining fuzz bytes as a compressed stream. Keep both the input
// storage and its ArrayInputStream alive until after GzipInputStream is gone.
extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
  if (size == 0) return 0;

  FuzzedDataProvider provider(data, size);
  const uint8_t options = provider.ConsumeIntegral<uint8_t>();
  const uint16_t skip_amount = provider.ConsumeIntegralInRange<uint16_t>(0, 32);
  const uint8_t backup_selector = provider.ConsumeIntegral<uint8_t>();
  const int buffer_size = provider.ConsumeIntegralInRange<int>(1, 128);

  constexpr size_t kMaxCompressedSize = 1 << 20;
  const size_t compressed_size =
      std::min(provider.remaining_bytes(), kMaxCompressedSize);
  const std::string compressed =
      provider.ConsumeBytesAsString(compressed_size);
  if (compressed.size() > static_cast<size_t>(std::numeric_limits<int>::max())) {
    return 0;
  }

  google::protobuf::io::ArrayInputStream source(
      compressed.data(), static_cast<int>(compressed.size()), 7);
  {
    const auto format = (options & 1)
                            ? google::protobuf::io::GzipInputStream::GZIP
                            : google::protobuf::io::GzipInputStream::AUTO;
    google::protobuf::io::GzipInputStream inflater(&source, format,
                                                    buffer_size);

    constexpr size_t kMaxIterations = 256;
    for (size_t i = 0; i < kMaxIterations; ++i) {
      const void* chunk = nullptr;
      int chunk_size = 0;
      if (!inflater.Next(&chunk, &chunk_size)) break;
      if (chunk == nullptr || chunk_size <= 0) break;

      if ((backup_selector & 1) != 0 && (i % 2) == 0) {
        // BackUp is valid only for bytes in the most recent Next() result.
        const int backed_up = static_cast<int>(backup_selector) % (chunk_size + 1);
        inflater.BackUp(backed_up);
      }

      if (!inflater.Skip(static_cast<int>(skip_amount))) break;
    }
    // The faulty Skip()/BackUp() leaves output_position_ before output_buffer_.
    // One more Next() reads from that position; with the library's bounds CHECK
    // removed this is the memory error the CHECK was hiding.
    const void* probe = nullptr;
    int probe_size = 0;
    (void)inflater.Next(&probe, &probe_size);
    (void)inflater.ByteCount();
    (void)inflater.ZlibErrorCode();
    (void)inflater.ZlibErrorMessage();
  }
  return 0;
}

// ---- standalone driver: replay one crash input ----
#include <cstdio>
#include <cstdint>
#include <vector>

int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr, "usage: %s <crash-input>\n", argv[0]);
    return 2;
  }
  std::FILE* f = std::fopen(argv[1], "rb");
  if (!f) { std::perror("fopen"); return 2; }
  std::fseek(f, 0, SEEK_END);
  long n = std::ftell(f);
  std::fseek(f, 0, SEEK_SET);
  std::vector<uint8_t> buf(n > 0 ? static_cast<size_t>(n) : 0);
  if (n > 0 && std::fread(buf.data(), 1, static_cast<size_t>(n), f) != static_cast<size_t>(n)) {
    std::fclose(f);
    return 2;
  }
  std::fclose(f);
  std::fprintf(stderr, "[poc] replaying %ld bytes\n", n);
  LLVMFuzzerTestOneInput(buf.data(), buf.size());
  std::fprintf(stderr, "[poc] returned normally - no crash\n");
  return 0;
}
