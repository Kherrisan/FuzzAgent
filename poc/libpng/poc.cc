#include <csetjmp>
#include <cstdio>
#include <vector>

#include <png.h>

static void on_info(png_structp png_ptr, png_infop) {
  png_start_read_image(png_ptr);
  (void)png_process_data_pause(png_ptr, 1);
}

static void on_row(png_structp png_ptr, png_bytep, png_uint_32, int) {
  (void)png_process_data_pause(png_ptr, 1);
}

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;

  FILE *file = std::fopen(argv[1], "rb");
  if (file == nullptr)
    return 2;
  std::fseek(file, 0, SEEK_END);
  const long size = std::ftell(file);
  std::rewind(file);
  if (size <= 0)
    return 2;

  std::vector<png_byte> input(static_cast<size_t>(size));
  if (std::fread(input.data(), 1, input.size(), file) != input.size()) {
    std::fclose(file);
    return 2;
  }
  std::fclose(file);

  png_structp png_ptr = png_create_read_struct(
      PNG_LIBPNG_VER_STRING, nullptr, nullptr, nullptr);
  if (png_ptr == nullptr)
    return 2;
  png_infop info_ptr = png_create_info_struct(png_ptr);
  if (info_ptr == nullptr) {
    png_destroy_read_struct(&png_ptr, nullptr, nullptr);
    return 2;
  }
  if (setjmp(png_jmpbuf(png_ptr)) != 0) {
    png_destroy_read_struct(&png_ptr, &info_ptr, nullptr);
    return 0;
  }

  png_set_progressive_read_fn(png_ptr, nullptr, on_info, on_row, nullptr);
  for (size_t offset = 0; offset < input.size(); ++offset)
    png_process_data(png_ptr, info_ptr, &input[offset], 1);

  png_destroy_read_struct(&png_ptr, &info_ptr, nullptr);
  return 0;
}
