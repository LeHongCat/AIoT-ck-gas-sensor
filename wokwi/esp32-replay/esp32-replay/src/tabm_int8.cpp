#include "tabm_int8.h"

#include <math.h>
#include <string.h>

namespace tabm {
namespace {

constexpr int kDeltaSteps[] = {5, 15, 30};  // d5, d15, d30 = last - value k steps earlier
constexpr float kGammaMax = 1.5f;           // ppm head clipped to 1.5 x the largest train set-point
constexpr float kCoDangerPpm = 200.0f;

// ONNX QuantizeLinear / DequantizeLinear: round half to even, saturate to int8.
inline int8_t quantize(float x, const QParam &q) {
  const float v = rintf(x / q.scale) + static_cast<float>(q.zero_point);
  return static_cast<int8_t>(v < -128.0f ? -128.0f : (v > 127.0f ? 127.0f : v));
}

inline float dequantize(int8_t v, const QParam &q) {
  return static_cast<float>(v - q.zero_point) * q.scale;
}

inline int32_t dot(const int8_t *a, const int8_t *b, int n) {
  int32_t s0 = 0, s1 = 0, s2 = 0, s3 = 0;
  for (int i = 0; i < n; i += 4) {  // n is a multiple of 4 (144, 1024)
    s0 += a[i] * b[i];
    s1 += a[i + 1] * b[i + 1];
    s2 += a[i + 2] * b[i + 2];
    s3 += a[i + 3] * b[i + 3];
  }
  return s0 + s1 + s2 + s3;
}

int32_t sum(const int8_t *a, int n) {
  int32_t s = 0;
  for (int i = 0; i < n; ++i) s += a[i];
  return s;
}

inline float softplus(float x) { return x > 20.0f ? x : log1pf(expf(x)); }

}  // namespace

void computeFeatures(const float window[kWindow][kSensors], float features[kFeatures]) {
  constexpr int kKinds = kFeatures / kSensors;  // 9
  static_assert(kKinds == 9, "last, mean, std, min, max, slope, d5, d15, d30");
  // Least-squares slope over t = 0..59 centred: sum((t - 29.5)^2) = 60 * (60^2 - 1) / 12.
  constexpr double kTMean = (kWindow - 1) / 2.0;
  constexpr double kTVar = kWindow * (static_cast<double>(kWindow) * kWindow - 1.0) / 12.0;

  for (int c = 0; c < kSensors; ++c) {
    float x[kWindow];
    for (int t = 0; t < kWindow; ++t) x[t] = (window[t][c] - kXMean[c]) / kXStd[c];

    double total = 0.0, weighted = 0.0;
    float low = x[0], high = x[0];
    for (int t = 0; t < kWindow; ++t) {
      total += x[t];
      weighted += x[t] * (t - kTMean);
      low = x[t] < low ? x[t] : low;
      high = x[t] > high ? x[t] : high;
    }
    const double mean = total / kWindow;
    double var = 0.0;
    for (int t = 0; t < kWindow; ++t) var += (x[t] - mean) * (x[t] - mean);

    const float last = x[kWindow - 1];
    const float kinds[kKinds] = {
        last,
        static_cast<float>(mean),
        static_cast<float>(sqrt(var / kWindow)),  // population std, like numpy
        low,
        high,
        static_cast<float>(weighted / kTVar),
        last - x[kWindow - 1 - kDeltaSteps[0]],
        last - x[kWindow - 1 - kDeltaSteps[1]],
        last - x[kWindow - 1 - kDeltaSteps[2]],
    };
    for (int k = 0; k < kKinds; ++k) {
      const int i = k * kSensors + c;
      features[i] = (kinds[k] - kFeatureMean[i]) / kFeatureStd[i];
    }
  }
}

bool Model::begin(const int8_t *blob, size_t size, int members) {
  if (blob == nullptr || size != kBlobSize || members < 1 || members > kMembers) return false;
  blob_ = blob;
  members_ = members;
  for (int b = 0; b < kBlocks; ++b) {
    const BlockParams &p = kBlockParams[b];
    for (int j = 0; j < kWidth; ++j) colsum_[b][j] = sum(blob_ + p.w_offset + j * p.din, p.din);
  }
  for (int i = 0; i < kMembers * kOutputs; ++i) {
    headColsum_[i] = sum(blob_ + kHead.w_offset + i * kWidth, kWidth);
  }
  return true;
}

void Model::forward(const float features[kFeatures], float raw[kOutputs], void (*yield)()) {
  int8_t input[kFeatures];
  for (int i = 0; i < kFeatures; ++i) input[i] = quantize(features[i], kInput);

  int8_t *act = bufferA_;  // [kMembers][din] after the element-wise r multiply
  int8_t *out = bufferB_;  // [kMembers][kWidth]
  QParam actQ = kInput;
  for (int b = 0; b < kBlocks; ++b) {
    const BlockParams &p = kBlockParams[b];
    const int8_t *r = blob_ + p.r_offset;
    const int8_t *s = blob_ + p.s_offset;
    const int8_t *bias = blob_ + p.bias_offset;

    // u = Q(act * r_m); block 0 starts from the input shared by every member.
    for (int m = 0; m < members_; ++m) {
      for (int i = 0; i < p.din; ++i) {
        const int8_t a = b == 0 ? input[i] : act[m * p.din + i];
        act[m * p.din + i] = quantize(dequantize(a, actQ) * dequantize(r[m * p.din + i], p.r), p.mul);
      }
    }

    // Each W column (a contiguous row of the transposed blob) is read once for all members.
    const float accScale = p.mul.scale * p.w_scale;
    const int32_t zeroCorrection = p.mul.zero_point;
    for (int j = 0; j < kWidth; ++j) {
      if (yield != nullptr && j % 64 == 0) yield();
      const int8_t *column = blob_ + p.w_offset + j * p.din;
      const int32_t correction = zeroCorrection * colsum_[b][j];
      for (int m = 0; m < members_; ++m) {
        const int32_t acc = dot(act + m * p.din, column, p.din) - correction;
        const int8_t v = quantize(static_cast<float>(acc) * accScale, p.matmul);
        const int k = m * kWidth + j;
        const int8_t w = quantize(dequantize(v, p.matmul) * dequantize(s[k], p.s), p.mul1);
        out[k] = quantize(dequantize(w, p.mul1) + dequantize(bias[k], p.bias), p.out);
      }
    }
    int8_t *swap = act;
    act = out;
    out = swap;
    actQ = p.out;
  }

  // Per-member head (1024 -> 10), then the mean over the members.
  const int8_t *headBias = blob_ + kHead.bias_offset;
  const float accScale = actQ.scale * kHead.w_scale;
  float total[kOutputs] = {};
  for (int m = 0; m < members_; ++m) {
    if (yield != nullptr && m % 8 == 0) yield();
    for (int o = 0; o < kOutputs; ++o) {
      const int idx = m * kOutputs + o;
      const int32_t acc = dot(act + m * kWidth, blob_ + kHead.w_offset + idx * kWidth, kWidth) -
                          actQ.zero_point * headColsum_[idx];
      const int8_t v = quantize(static_cast<float>(acc) * accScale, kHead.matmul);
      const int8_t y = quantize(dequantize(v, kHead.matmul) + dequantize(headBias[idx], kHead.bias),
                                kHead.add);
      total[o] += dequantize(y, kHead.add);
    }
  }
  for (int o = 0; o < kOutputs; ++o) raw[o] = total[o] / members_;
}

void decode(const float raw[kOutputs], Estimate &e) {
  for (int g = 0; g < kGases; ++g) {
    const float logit = raw[0 * kGases + g];
    const float gamma = raw[1 * kGases + g];
    const float nu = softplus(raw[2 * kGases + g]) + 1e-6f;
    const float alpha = softplus(raw[3 * kGases + g]) + 1.0f;
    const float beta = softplus(raw[4 * kGases + g]) + 1e-6f;
    e.prob[g] = 1.0f / (1.0f + expf(-logit));
    e.present[g] = e.prob[g] >= kTau[g];
    const float clipped = gamma < 0.0f ? 0.0f : (gamma > kGammaMax ? kGammaMax : gamma);
    e.ppm[g] = e.present[g] ? clipped * kYMax[g] : 0.0f;
    const float scale = sqrtf(beta * (1.0f + nu) / (alpha * nu));
    e.uctPpm[g] = (scale + 1.0f / sqrtf(nu)) * kYMax[g];
    e.uncertain[g] = e.present[g] && e.uctPpm[g] > kUctCut[g];
  }
}

const char *alarmName(AlarmLevel level) {
  switch (level) {
    case AlarmLevel::Danger:
      return "danger";
    case AlarmLevel::Warning:
      return "warning";
    default:
      return "normal";
  }
}

AlarmLevel AlarmFilter::update(const Estimate &e, uint64_t sequence) {
  memcpy(prob_[next_], e.prob, sizeof(e.prob));
  coPpm_[next_] = e.ppm[0];
  sequence_[next_] = sequence;
  next_ = (next_ + 1) % kSmooth;
  count_ = count_ < kSmooth ? count_ + 1 : kSmooth;

  float co = 0.0f, probCo = 0.0f;
  int used = 0;
  for (int i = 0; i < count_; ++i) {
    if (sequence_[i] + kSmooth <= sequence) continue;  // older than the last 5 s
    co += coPpm_[i];
    probCo += prob_[i][0];
    ++used;
  }
  if (co / used >= kCoDangerPpm) return AlarmLevel::Danger;  // used >= 1: this estimate
  if (probCo / used >= kTau[0]) return AlarmLevel::Warning;
  return AlarmLevel::Normal;
}

}  // namespace tabm
