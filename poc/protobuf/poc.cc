// Standalone reproducer for GzipInputStream::Skip() with input.bin.
#include <cstdio>
#include <cstddef>
#include <cstdint>
#include <vector>

#include <google/protobuf/io/gzip_stream.h>
#include <google/protobuf/io/zero_copy_stream_impl_lite.h>

int main(int argc, char** argv) {
  if (argc != 2) {
    std::fprintf(stderr, "usage: %s <input>\n", argv[0]);
    return 2;
  }

  std::FILE* file = std::fopen(argv[1], "rb");
  if (file == nullptr) {
    std::perror("fopen");
    return 2;
  }
  if (std::fseek(file, 0, SEEK_END) != 0) {
    std::fclose(file);
    return 2;
  }
  const long file_size = std::ftell(file);
  std::rewind(file);
  if (file_size < 5) {
    std::fclose(file);
    return 2;
  }

  std::vector<uint8_t> input(static_cast<size_t>(file_size));
  if (std::fread(input.data(), 1, input.size(), file) != input.size()) {
    std::fclose(file);
    return 2;
  }
  std::fclose(file);

  // The original fuzz harness consumed four control bytes from the end of
  // this fixture. These are the resulting settings; the remaining prefix is
  // passed to the gzip stream as input.
  constexpr int kSkipAmount = 18;
  constexpr int kBackupSelector = 121;
  constexpr int kBufferSize = 102;
  const size_t compressed_size = input.size() - 4;
  std::fprintf(stderr, "[poc] replaying %zu compressed bytes\n", compressed_size);

  google::protobuf::io::ArrayInputStream source(
      input.data(), static_cast<int>(compressed_size), 7);
  google::protobuf::io::GzipInputStream inflater(
      &source, google::protobuf::io::GzipInputStream::AUTO, kBufferSize);

  constexpr size_t kMaxIterations = 256;
  for (size_t i = 0; i < kMaxIterations; ++i) {
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

  (void)inflater.ByteCount();
  (void)inflater.ZlibErrorCode();
  (void)inflater.ZlibErrorMessage();
  return 0;
}
