// Checks src/tabm_int8.cpp on a PC against onnxruntime (host_test/golden.bin, written by
// tools/export_tabm_esp32.py). Build from the firmware folder, e.g. with zig:
//
//   python -m ziglang c++ -O2 -std=c++17 -Isrc host_test/host_test.cpp src/tabm_int8.cpp -o host_test/host_test.exe
//   host_test/host_test.exe model/tabm_int8.bin host_test/golden.bin
#include <math.h>
#include <stdio.h>
#include <string.h>

#include <chrono>
#include <vector>

#include "tabm_int8.h"

namespace {

bool readFile(const char *path, std::vector<char> &data) {
  FILE *f = fopen(path, "rb");
  if (f == nullptr) return false;
  fseek(f, 0, SEEK_END);
  data.resize(static_cast<size_t>(ftell(f)));
  fseek(f, 0, SEEK_SET);
  const bool ok = fread(data.data(), 1, data.size(), f) == data.size();
  fclose(f);
  return ok;
}

float maxAbsDiff(const float *a, const float *b, int n) {
  float worst = 0.0f;
  for (int i = 0; i < n; ++i) worst = fmaxf(worst, fabsf(a[i] - b[i]));
  return worst;
}

tabm::Model model;        // ~85 KB of buffers each: keep them off the stack
tabm::Model subsetModel;  // the first `subset` members only, as the firmware may run

tabm::Estimate estimate(float pCo, float coPpm) {
  tabm::Estimate e = {};
  e.prob[0] = pCo;
  e.ppm[0] = coPpm;
  return e;
}

// The alarm averages the last 5 s of readings, whether predictions come every second or not.
bool alarmChecks() {
  tabm::AlarmFilter dense;
  bool ok = true;
  for (uint64_t s = 0; s < 4; ++s) ok &= dense.update(estimate(0.1f, 0.0f), s) == tabm::AlarmLevel::Normal;
  // One 300 ppm second among four clean ones: the 5 s mean (60 ppm, P 0.24) stays normal.
  ok &= dense.update(estimate(0.82f, 300.0f), 4) == tabm::AlarmLevel::Normal;
  for (uint64_t s = 5; s < 8; ++s) dense.update(estimate(0.95f, 300.0f), s);
  ok &= dense.update(estimate(0.95f, 300.0f), 8) == tabm::AlarmLevel::Danger;

  tabm::AlarmFilter sparse;  // ~38 s between predictions, as in Wokwi
  ok &= sparse.update(estimate(0.15f, 0.0f), 59) == tabm::AlarmLevel::Normal;
  ok &= sparse.update(estimate(0.16f, 0.0f), 90) == tabm::AlarmLevel::Normal;
  ok &= sparse.update(estimate(0.95f, 277.6f), 123) == tabm::AlarmLevel::Danger;

  tabm::AlarmFilter ethylene;  // C2H4 alone is reported but raises no alarm
  tabm::Estimate e = estimate(0.05f, 0.0f);
  e.prob[1] = 0.95f;
  e.ppm[1] = 9.0f;
  for (uint64_t s = 0; s < 10; s += 5) ok &= ethylene.update(e, s) == tabm::AlarmLevel::Normal;
  return ok;
}

}  // namespace

int main(int argc, char **argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: %s model/tabm_int8.bin host_test/golden.bin\n", argv[0]);
    return 2;
  }
  std::vector<char> blob, golden;
  if (!readFile(argv[1], blob) || !readFile(argv[2], golden)) {
    fprintf(stderr, "cannot read the input files\n");
    return 2;
  }
  if (!model.begin(reinterpret_cast<const int8_t *>(blob.data()), blob.size())) {
    fprintf(stderr, "blob size %zu != kBlobSize %zu: rerun the export\n", blob.size(), tabm::kBlobSize);
    return 2;
  }
  uint32_t count = 0, subset = 0;
  constexpr size_t kHeader = 16;  // "TABMGLD2", window count, members of the subset outputs
  if (golden.size() < kHeader || memcmp(golden.data(), "TABMGLD2", 8) != 0) {
    fprintf(stderr, "not a golden file (rerun the export)\n");
    return 2;
  }
  memcpy(&count, golden.data() + 8, 4);
  memcpy(&subset, golden.data() + 12, 4);
  constexpr size_t kRecord =
      (tabm::kWindow * tabm::kSensors + tabm::kFeatures + 2 * tabm::kOutputs) * 4;
  if (golden.size() != kHeader + count * kRecord ||
      !subsetModel.begin(reinterpret_cast<const int8_t *>(blob.data()), blob.size(),
                         static_cast<int>(subset))) {
    fprintf(stderr, "golden file size or subset mismatch\n");
    return 2;
  }

  float worstFeature = 0.0f, worstRaw = 0.0f, worstRawOwn = 0.0f, worstSubset = 0.0f;
  int decisionsDiffer = 0, subsetDecisionsDiffer = 0;
  double totalMs = 0.0, subsetMs = 0.0;
  for (uint32_t k = 0; k < count; ++k) {
    const char *record = golden.data() + kHeader + k * kRecord;
    float window[tabm::kWindow][tabm::kSensors], zRef[tabm::kFeatures], rawRef[tabm::kOutputs];
    float rawSubsetRef[tabm::kOutputs];
    memcpy(window, record, sizeof(window));
    memcpy(zRef, record + sizeof(window), sizeof(zRef));
    memcpy(rawRef, record + sizeof(window) + sizeof(zRef), sizeof(rawRef));
    memcpy(rawSubsetRef, record + sizeof(window) + sizeof(zRef) + sizeof(rawRef),
           sizeof(rawSubsetRef));

    float z[tabm::kFeatures], raw[tabm::kOutputs], rawOwn[tabm::kOutputs];
    tabm::computeFeatures(window, z);
    worstFeature = fmaxf(worstFeature, maxAbsDiff(z, zRef, tabm::kFeatures));

    model.forward(zRef, raw);  // same input as onnxruntime: isolates the kernels
    worstRaw = fmaxf(worstRaw, maxAbsDiff(raw, rawRef, tabm::kOutputs));

    const auto started = std::chrono::steady_clock::now();
    model.forward(z, rawOwn);  // the full on-device path
    totalMs += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
    worstRawOwn = fmaxf(worstRawOwn, maxAbsDiff(rawOwn, rawRef, tabm::kOutputs));

    tabm::Estimate mine, ref;
    tabm::decode(rawOwn, mine);
    tabm::decode(rawRef, ref);
    for (int g = 0; g < tabm::kGases; ++g) decisionsDiffer += mine.present[g] != ref.present[g];

    float rawSubset[tabm::kOutputs];  // first `subset` members vs the numpy integer kernels
    const auto subsetStarted = std::chrono::steady_clock::now();
    subsetModel.forward(z, rawSubset);
    subsetMs += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() -
                                                          subsetStarted).count();
    worstSubset = fmaxf(worstSubset, maxAbsDiff(rawSubset, rawSubsetRef, tabm::kOutputs));
    tabm::Estimate mineSubset, refSubset;
    tabm::decode(rawSubset, mineSubset);
    tabm::decode(rawSubsetRef, refSubset);
    for (int g = 0; g < tabm::kGases; ++g) {
      subsetDecisionsDiffer += mineSubset.present[g] != refSubset.present[g];
    }
  }

  printf("%u golden windows\n", count);
  printf("  features     max |C - numpy|        %.2e\n", worstFeature);
  printf("  raw outputs  max |C - onnxruntime|  %.4f (same features)\n", worstRaw);
  printf("  raw outputs  max |C - onnxruntime|  %.4f (C features)\n", worstRawOwn);
  printf("  presence decisions that differ      %d / %u\n", decisionsDiffer, count * tabm::kGases);
  printf("  forward pass on this PC             %.2f ms\n", totalMs / count);
  printf("  %2u members: max |C - numpy int|    %.4f, decisions that differ %d / %u\n", subset,
         worstSubset, subsetDecisionsDiffer, count * tabm::kGases);
  printf("  %2u members: forward pass on this PC %.2f ms\n", subset, subsetMs / count);
  const bool alarmOk = alarmChecks();
  printf("  alarm filter (dense, sparse, C2H4)  %s\n", alarmOk ? "ok" : "WRONG");
  const bool ok = worstFeature < 1e-3f && worstRaw < 0.1f && worstRawOwn < 0.1f &&
                  decisionsDiffer == 0 && worstSubset < 0.1f && subsetDecisionsDiffer == 0 &&
                  alarmOk;
  printf("%s\n", ok ? "PASS" : "FAIL");
  return ok ? 0 : 1;
}
