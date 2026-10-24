// TabM INT8 edge model: 60 s window of 16 MOX channels -> P(CO), P(C2H4), ppm, alarm level.
//
//   raw window ─► (x - x_mean) / x_std ─► 144 features ─► standardise ─► TabM INT8 ─► decode ─► alarm
//
// Portable C++ (no Arduino), so tools/export_tabm_esp32.py's golden data can check it on a PC:
// see host_test/host_test.cpp. Matches tools/tabm_reference.py.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "tabm_model_params.h"

namespace tabm {

// window[t][c]: raw sensor values, oldest first, exactly kWindow readings.
// features: the standardised model input (kind-major: last, mean, std, min, max, slope, d5, d15, d30).
void computeFeatures(const float window[kWindow][kSensors], float features[kFeatures]);

class Model {
 public:
  // blob: model/tabm_int8.bin (kBlobSize bytes), e.g. embedded in flash.
  // members: run only the first 1..kMembers ensemble members (cost scales with it) and average
  // those; kMembers is the benchmarked model.
  bool begin(const int8_t *blob, size_t size, int members = kMembers);
  int members() const { return members_; }
  // features -> the 10 raw outputs, layout [5][kGases]: logit, gamma, nu, alpha, beta.
  // yield (optional) runs every few hundred thousand MACs, e.g. to let FreeRTOS feed the watchdog.
  void forward(const float features[kFeatures], float raw[kOutputs], void (*yield)() = nullptr);

 private:
  const int8_t *blob_ = nullptr;
  int members_ = kMembers;
  int32_t colsum_[kBlocks][kWidth] = {};       // zero-point correction of each matmul column
  int32_t headColsum_[kMembers * kOutputs] = {};
  int8_t bufferA_[kMembers * kWidth] = {};
  int8_t bufferB_[kMembers * kWidth] = {};
};

struct Estimate {
  float prob[kGases];     // P(gas present)
  bool present[kGases];   // prob >= tau
  float ppm[kGases];      // hurdle estimate: 0 when predicted absent
  float uctPpm[kGases];   // DER uncertainty (aleatoric scale + epistemic), ppm
  bool uncertain[kGases]; // present but uctPpm above the benchmark's cut
};

void decode(const float raw[kOutputs], Estimate &estimate);

enum class AlarmLevel { Normal, Warning, Danger };
const char *alarmName(AlarmLevel level);

// Causal means over the last 5 s of readings: WARNING when mean P(CO) >= tau, DANGER when mean CO
// >= 200 ppm (the notebook's CO alarm). C2H4 is estimated and reported but raises no alarm.
// sequence = the 1 Hz reading that ended the window, so sparse predictions (one every few
// seconds) are not averaged over minutes.
class AlarmFilter {
 public:
  AlarmLevel update(const Estimate &estimate, uint64_t sequence);

 private:
  static constexpr int kSmooth = 5;
  float prob_[kSmooth][kGases] = {};
  float coPpm_[kSmooth] = {};
  uint64_t sequence_[kSmooth] = {};
  int count_ = 0;
  int next_ = 0;
};

}  // namespace tabm
