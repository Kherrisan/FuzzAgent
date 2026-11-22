# libvpx VP9 SVC pending-frame array out-of-bounds access

## Summary

The VP9 encoder can write past its fixed-size `pending_frame_sizes` array when encoding with spatial and temporal scalability (SVC). The array holds eight frame sizes, but two insertion paths in `vp9_cx_iface.c` increment `pending_frame_count` without checking that the next slot is available. The supplied proof of concept configures two spatial layers and two temporal layers and triggers an out-of-bounds write. Subsequent packet retrieval can read outside the encoder's heap allocation.

The issue is in the encoder API path. It is not triggered by parsing a specially constructed VP9 bitstream. The included `input.bin` supplies pixel values for the frames; the encoder configuration and frame sequence are what exercise the faulty bookkeeping.

## Details

`pending_frame_sizes` is declared with eight entries in `vp9/vp9_cx_iface.c`:

```c
size_t pending_frame_sizes[8];
```

While collecting encoded SVC layers, the encoder records each non-empty frame size. The relevant insertion operations are:

```c
if (size) ctx->pending_frame_sizes[ctx->pending_frame_count++] = size;
```

They occur in both the path that accumulates invisible or lower spatial-layer frames and the path that completes a returned frame packet. Neither checks `pending_frame_count` against the array capacity before indexing. When the count is already eight, the next insertion writes to `pending_frame_sizes[8]`, one element beyond the array. `write_superframe_index()` also assumes the count is at most eight; its assertion does not protect the earlier insertions and may be disabled in non-assert builds.

The proof of concept enables two spatial layers and two temporal layers, assigns SVC layer IDs, encodes two frames, and retrieves the encoded packets with `vpx_codec_get_cx_data()`. With the included fixture, UBSan reports the index-8 write. ASan then reports a heap-buffer-overflow during packet retrieval, when the corrupted bookkeeping is used. The concrete PoC and fixture are in this project directory as `poc.cc` and `input.bin`.

## Reproduction

The essential setup is a VP9 encoder configured for two spatial and two temporal layers. The complete standalone harness, including image allocation, input loading, and error handling, is in `poc.cc`.

```cpp
cfg.ss_number_layers = 2;
cfg.ts_number_layers = 2;
cfg.ts_rate_decimator[0] = 2;
cfg.ts_rate_decimator[1] = 1;
cfg.ts_periodicity = 2;
cfg.temporal_layering_mode = VP9E_TEMPORAL_LAYERING_MODE_BYPASS;

vpx_svc_extra_cfg_t svc = {};
svc.temporal_layering_mode = VP9E_TEMPORAL_LAYERING_MODE_BYPASS;
for (unsigned int sl = 0; sl < 2; ++sl) {
  svc.scaling_factor_num[sl] = 1;
  svc.scaling_factor_den[sl] = sl == 0 ? 2 : 1;
  svc.speed_per_layer[sl] = 5;
}
vpx_codec_control_(&ctx, VP9E_SET_SVC, 1);
vpx_codec_control_(&ctx, VP9E_SET_SVC_PARAMETERS, &svc);

for (unsigned int frame = 0; frame < 2; ++frame) {
  vpx_svc_layer_id_t id = {};
  id.spatial_layer_id = frame & 1;
  id.temporal_layer_id = frame & 1;
  for (int sl = 0; sl < 2; ++sl)
    id.temporal_layer_id_per_spatial[sl] = (frame + sl) & 1;
  vpx_codec_control_(&ctx, VP9E_SET_SVC_LAYER_ID, &id);
  vpx_codec_encode(&ctx, &image, frame, 1, 0, VPX_DL_REALTIME);

  vpx_codec_iter_t iter = nullptr;
  while (vpx_codec_get_cx_data(&ctx, &iter) != nullptr) {}
}
```

Build against the ASan/UBSan-instrumented libvpx output. Set `OUT` to the campaign output directory containing `build/sanitizer/include` and `build/sanitizer/lib`, then run:

```sh
cd poc/libvpx
export OUT=<libvpx-campaign-output>
bash build.sh
./poc input.bin
```

The build links `libvpx.a` with AddressSanitizer and UndefinedBehaviorSanitizer enabled. On the affected build, the expected diagnostics include `index 8 out of bounds for type 'size_t[8]'` from UBSan and a heap-buffer-overflow report from ASan in `vpx_codec_get_cx_data()`.

## Security impact

The demonstrated impact is memory-safety corruption in the VP9 encoder and a process crash under sanitizers. The first fault is an out-of-bounds write; the subsequent packet retrieval can perform an out-of-bounds read. This can cause denial of service in an application that uses the affected encoder with the triggering SVC configuration. The PoC does not demonstrate code execution, and the issue is not directly reachable by submitting an encoded VP9 bitstream to a decoder. Whether an external party can trigger it depends on whether that party can influence frames sent to an application configured to encode VP9 with this SVC setup.

## git describe

The affected libvpx source checkout used for this reproduction reports:

```text
e019cff
```

This is the output of `git describe --always` for that checkout.
