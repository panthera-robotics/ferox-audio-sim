#include <api/audio/echo_canceller3_config.h>
#include <modules/audio_processing/include/audio_processing.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {

constexpr std::uint32_t kSampleRate = 48'000U;
constexpr std::uint16_t kChannels = 1U;
constexpr std::uint16_t kBitsPerSample = 16U;
constexpr std::size_t kSamplesPerFrame = 480U;
constexpr std::size_t kBytesPerFrame = kSamplesPerFrame * sizeof(std::int16_t);
constexpr std::uint64_t kMaximumDurationSeconds = 600U;
constexpr std::uint64_t kMaximumPcmBytes =
    kSampleRate * kChannels * (kBitsPerSample / 8U) * kMaximumDurationSeconds;

struct Options {
  std::filesystem::path render_wav;
  std::filesystem::path capture_wav;
  std::filesystem::path output_wav;
  std::filesystem::path report;
  int stream_delay_ms = 0;
};

struct AecProfile {
  std::string name;
  std::string tuning_source;
  webrtc::EchoCanceller3Config config;
};

AecProfile DefaultAecProfile() {
  AecProfile profile;
  profile.name = "default";
  profile.tuning_source = "WebRTC M131 default EchoCanceller3Config";
  if (!webrtc::EchoCanceller3Config::Validate(&profile.config)) {
    throw std::runtime_error("default AEC3 profile failed strict validation");
  }
  return profile;
}

struct WavData {
  std::vector<std::int16_t> samples;
};

class ExclusiveFile {
 public:
  explicit ExclusiveFile(const std::filesystem::path &path) : path_(path) {
    if (path.empty() || std::filesystem::is_symlink(path)) {
      throw std::runtime_error("output path is empty or a symlink: " + path.string());
    }
    const int descriptor = ::open(
        path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, S_IRUSR | S_IWUSR);
    if (descriptor < 0) {
      throw std::runtime_error(
          "cannot create output without overwrite: " + path.string() + ": " +
          std::strerror(errno));
    }
    handle_ = ::fdopen(descriptor, "wb");
    if (handle_ == nullptr) {
      ::close(descriptor);
      ::unlink(path.c_str());
      throw std::runtime_error("cannot open output stream: " + path.string());
    }
  }

  ExclusiveFile(const ExclusiveFile &) = delete;
  ExclusiveFile &operator=(const ExclusiveFile &) = delete;

  ~ExclusiveFile() {
    if (handle_ != nullptr) {
      std::fclose(handle_);
    }
    if (!committed_) {
      ::unlink(path_.c_str());
    }
  }

  void Write(const void *payload, std::size_t size) {
    if (size > 0U && std::fwrite(payload, 1U, size, handle_) != size) {
      throw std::runtime_error("short write: " + path_.string());
    }
  }

  void Write(const std::string &payload) { Write(payload.data(), payload.size()); }

  void Flush() {
    if (std::fflush(handle_) != 0 || ::fsync(::fileno(handle_)) != 0) {
      throw std::runtime_error("cannot flush output: " + path_.string());
    }
  }

  void Commit() noexcept { committed_ = true; }

 private:
  std::filesystem::path path_;
  std::FILE *handle_ = nullptr;
  bool committed_ = false;
};

std::string RequireValue(int argc, char **argv, int *index) {
  if (*index + 1 >= argc) {
    throw std::runtime_error(std::string("missing value after ") + argv[*index]);
  }
  ++(*index);
  return argv[*index];
}

Options ParseOptions(int argc, char **argv) {
  Options options;
  for (int index = 1; index < argc; ++index) {
    const std::string argument = argv[index];
    if (argument == "--render-wav") {
      options.render_wav = RequireValue(argc, argv, &index);
    } else if (argument == "--capture-wav") {
      options.capture_wav = RequireValue(argc, argv, &index);
    } else if (argument == "--output-wav") {
      options.output_wav = RequireValue(argc, argv, &index);
    } else if (argument == "--report") {
      options.report = RequireValue(argc, argv, &index);
    } else if (argument == "--stream-delay-ms") {
      options.stream_delay_ms = std::stoi(RequireValue(argc, argv, &index));
    } else {
      throw std::runtime_error("unknown argument: " + argument);
    }
  }
  if (
      options.render_wav.empty() || options.capture_wav.empty() ||
      options.output_wav.empty() || options.report.empty()) {
    throw std::runtime_error(
        "--render-wav, --capture-wav, --output-wav, and --report are required");
  }
  if (options.stream_delay_ms < 0 || options.stream_delay_ms > 500) {
    throw std::runtime_error("--stream-delay-ms must be in [0, 500]");
  }
  const std::array<std::filesystem::path, 4U> paths = {
      std::filesystem::absolute(options.render_wav).lexically_normal(),
      std::filesystem::absolute(options.capture_wav).lexically_normal(),
      std::filesystem::absolute(options.output_wav).lexically_normal(),
      std::filesystem::absolute(options.report).lexically_normal(),
  };
  for (std::size_t left = 0U; left < paths.size(); ++left) {
    for (std::size_t right = left + 1U; right < paths.size(); ++right) {
      if (paths[left] == paths[right]) {
        throw std::runtime_error("all input and output paths must be distinct");
      }
    }
  }
  return options;
}

std::vector<std::uint8_t> ReadRegularFile(
    const std::filesystem::path &path, std::uint64_t maximum_bytes) {
  if (path.empty() || std::filesystem::is_symlink(path)) {
    throw std::runtime_error("input must be a regular non-symlink file: " + path.string());
  }
  const int descriptor = ::open(path.c_str(), O_RDONLY | O_NOFOLLOW);
  if (descriptor < 0) {
    throw std::runtime_error("cannot open input: " + path.string() + ": " +
                             std::strerror(errno));
  }
  struct stat status {};
  if (::fstat(descriptor, &status) != 0 || !S_ISREG(status.st_mode) ||
      status.st_size <= 0 || static_cast<std::uint64_t>(status.st_size) > maximum_bytes) {
    ::close(descriptor);
    throw std::runtime_error("input size or type is invalid: " + path.string());
  }
  std::vector<std::uint8_t> payload(static_cast<std::size_t>(status.st_size));
  std::size_t offset = 0U;
  while (offset < payload.size()) {
    const ssize_t received = ::read(
        descriptor, payload.data() + offset, payload.size() - offset);
    if (received <= 0) {
      ::close(descriptor);
      throw std::runtime_error("short read: " + path.string());
    }
    offset += static_cast<std::size_t>(received);
  }
  ::close(descriptor);
  return payload;
}

std::uint16_t ReadLe16(const std::uint8_t *data) {
  return static_cast<std::uint16_t>(data[0]) |
         static_cast<std::uint16_t>(static_cast<std::uint16_t>(data[1]) << 8U);
}

std::uint32_t ReadLe32(const std::uint8_t *data) {
  return static_cast<std::uint32_t>(data[0]) |
         (static_cast<std::uint32_t>(data[1]) << 8U) |
         (static_cast<std::uint32_t>(data[2]) << 16U) |
         (static_cast<std::uint32_t>(data[3]) << 24U);
}

bool ChunkIdEquals(const std::uint8_t *data, const char *expected) {
  return std::memcmp(data, expected, 4U) == 0;
}

WavData ParseWav(const std::filesystem::path &path) {
  const auto payload = ReadRegularFile(path, kMaximumPcmBytes + 1'000'000U);
  if (
      payload.size() < 44U || !ChunkIdEquals(payload.data(), "RIFF") ||
      !ChunkIdEquals(payload.data() + 8U, "WAVE")) {
    throw std::runtime_error("input is not a RIFF/WAVE file: " + path.string());
  }
  const std::uint32_t riff_size = ReadLe32(payload.data() + 4U);
  if (static_cast<std::uint64_t>(riff_size) + 8U != payload.size()) {
    throw std::runtime_error("WAV RIFF size is inconsistent: " + path.string());
  }
  bool format_seen = false;
  bool data_seen = false;
  std::vector<std::uint8_t> pcm;
  std::size_t offset = 12U;
  while (offset < payload.size()) {
    if (payload.size() - offset < 8U) {
      throw std::runtime_error("truncated WAV chunk header: " + path.string());
    }
    const std::uint8_t *header = payload.data() + offset;
    const std::uint32_t chunk_size = ReadLe32(header + 4U);
    offset += 8U;
    if (static_cast<std::uint64_t>(chunk_size) > payload.size() - offset) {
      throw std::runtime_error("truncated WAV chunk: " + path.string());
    }
    if (ChunkIdEquals(header, "fmt ")) {
      if (format_seen || chunk_size < 16U) {
        throw std::runtime_error("invalid or duplicate WAV fmt chunk: " + path.string());
      }
      const std::uint8_t *format = payload.data() + offset;
      if (
          ReadLe16(format) != 1U || ReadLe16(format + 2U) != kChannels ||
          ReadLe32(format + 4U) != kSampleRate ||
          ReadLe32(format + 8U) != kSampleRate * 2U ||
          ReadLe16(format + 12U) != 2U ||
          ReadLe16(format + 14U) != kBitsPerSample) {
        throw std::runtime_error(
            "WAV must be 48 kHz mono PCM16: " + path.string());
      }
      format_seen = true;
    } else if (ChunkIdEquals(header, "data")) {
      if (data_seen || chunk_size == 0U || chunk_size > kMaximumPcmBytes) {
        throw std::runtime_error("invalid or duplicate WAV data chunk: " + path.string());
      }
      pcm.assign(payload.begin() + static_cast<std::ptrdiff_t>(offset),
                 payload.begin() + static_cast<std::ptrdiff_t>(offset + chunk_size));
      data_seen = true;
    }
    offset += chunk_size;
    if ((chunk_size & 1U) != 0U) {
      if (offset >= payload.size()) {
        throw std::runtime_error("missing WAV chunk padding: " + path.string());
      }
      ++offset;
    }
  }
  if (!format_seen || !data_seen || pcm.size() % kBytesPerFrame != 0U) {
    throw std::runtime_error(
        "WAV lacks required chunks or complete 10 ms frames: " + path.string());
  }
  WavData result;
  result.samples.reserve(pcm.size() / 2U);
  for (std::size_t index = 0U; index < pcm.size(); index += 2U) {
    const std::uint16_t encoded = ReadLe16(pcm.data() + index);
    const std::int32_t decoded = encoded >= 0x8000U
                                     ? static_cast<std::int32_t>(encoded) - 65'536
                                     : static_cast<std::int32_t>(encoded);
    result.samples.push_back(static_cast<std::int16_t>(decoded));
  }
  return result;
}

void AppendLe16(std::vector<std::uint8_t> *output, std::uint16_t value) {
  output->push_back(static_cast<std::uint8_t>(value & 0xffU));
  output->push_back(static_cast<std::uint8_t>((value >> 8U) & 0xffU));
}

void AppendLe32(std::vector<std::uint8_t> *output, std::uint32_t value) {
  for (unsigned int shift = 0U; shift < 32U; shift += 8U) {
    output->push_back(static_cast<std::uint8_t>((value >> shift) & 0xffU));
  }
}

void AppendTag(std::vector<std::uint8_t> *output, const char *tag) {
  output->insert(output->end(), tag, tag + 4U);
}

void WriteWav(
    const std::filesystem::path &path, const std::vector<std::int16_t> &samples) {
  const std::uint64_t pcm_size_64 = samples.size() * sizeof(std::int16_t);
  if (pcm_size_64 == 0U || pcm_size_64 > std::numeric_limits<std::uint32_t>::max() - 36U) {
    throw std::runtime_error("output WAV size is invalid");
  }
  const auto pcm_size = static_cast<std::uint32_t>(pcm_size_64);
  std::vector<std::uint8_t> header;
  header.reserve(44U);
  AppendTag(&header, "RIFF");
  AppendLe32(&header, 36U + pcm_size);
  AppendTag(&header, "WAVE");
  AppendTag(&header, "fmt ");
  AppendLe32(&header, 16U);
  AppendLe16(&header, 1U);
  AppendLe16(&header, kChannels);
  AppendLe32(&header, kSampleRate);
  AppendLe32(&header, kSampleRate * 2U);
  AppendLe16(&header, 2U);
  AppendLe16(&header, kBitsPerSample);
  AppendTag(&header, "data");
  AppendLe32(&header, pcm_size);
  ExclusiveFile output(path);
  output.Write(header.data(), header.size());
  std::array<std::uint8_t, 2U> encoded {};
  for (const std::int16_t sample : samples) {
    const auto bits = static_cast<std::uint16_t>(sample);
    encoded[0] = static_cast<std::uint8_t>(bits & 0xffU);
    encoded[1] = static_cast<std::uint8_t>((bits >> 8U) & 0xffU);
    output.Write(encoded.data(), encoded.size());
  }
  output.Flush();
  output.Commit();
}

template <typename T>
std::string JsonOptional(const std::optional<T> &value) {
  if (!value.has_value()) {
    return "null";
  }
  std::ostringstream stream;
  if constexpr (std::is_floating_point_v<T>) {
    if (!std::isfinite(*value)) {
      return "null";
    }
    stream << std::setprecision(12) << *value;
  } else {
    stream << *value;
  }
  return stream.str();
}

std::string BuildReport(
    std::size_t frame_count,
    int stream_delay_ms,
    double processing_seconds,
    const AecProfile &profile,
    const webrtc::AudioProcessingStats &stats) {
  const double audio_duration_seconds =
      static_cast<double>(frame_count * kSamplesPerFrame) /
      static_cast<double>(kSampleRate);
  std::ostringstream stream;
  stream << std::fixed << std::setprecision(9)
         << "{\n"
         << "  \"aec_algorithm\": \"WebRTC AEC3\",\n"
         << "  \"aec_enabled\": true,\n"
         << "  \"aec_profile\": \"" << profile.name << "\",\n"
         << "  \"agc_enabled\": false,\n"
         << "  \"audio_duration_s\": " << audio_duration_seconds << ",\n"
         << "  \"channels\": 1,\n"
         << "  \"control_authorized\": false,\n"
         << "  \"delay_median_ms\": "
         << JsonOptional(stats.delay_median_ms) << ",\n"
         << "  \"delay_ms\": " << JsonOptional(stats.delay_ms) << ",\n"
         << "  \"delay_standard_deviation_ms\": "
         << JsonOptional(stats.delay_standard_deviation_ms) << ",\n"
         << "  \"divergent_filter_fraction\": "
         << JsonOptional(stats.divergent_filter_fraction) << ",\n"
         << "  \"echo_return_loss_db\": "
         << JsonOptional(stats.echo_return_loss) << ",\n"
         << "  \"echo_return_loss_enhancement_db\": "
         << JsonOptional(stats.echo_return_loss_enhancement) << ",\n"
         << "  \"frame_count\": " << frame_count << ",\n"
         << "  \"frame_duration_ms\": 10,\n"
         << "  \"high_pass_filter_enabled\": false,\n"
         << "  \"noise_suppression_enabled\": false,\n"
         << "  \"nearend_detection_enr_threshold\": "
         << profile.config.suppressor.dominant_nearend_detection.enr_threshold
         << ",\n"
         << "  \"nearend_mask_hf_enr_suppress\": "
         << profile.config.suppressor.nearend_tuning.mask_hf.enr_suppress
         << ",\n"
         << "  \"nearend_mask_hf_enr_transparent\": "
         << profile.config.suppressor.nearend_tuning.mask_hf.enr_transparent
         << ",\n"
         << "  \"nearend_mask_lf_enr_suppress\": "
         << profile.config.suppressor.nearend_tuning.mask_lf.enr_suppress
         << ",\n"
         << "  \"nearend_mask_lf_enr_transparent\": "
         << profile.config.suppressor.nearend_tuning.mask_lf.enr_transparent
         << ",\n"
         << "  \"offline_only\": true,\n"
         << "  \"pcm_format\": \"S16_LE\",\n"
         << "  \"processing_elapsed_s\": " << processing_seconds << ",\n"
         << "  \"production_ready\": false,\n"
         << "  \"realtime_factor\": "
         << processing_seconds / audio_duration_seconds << ",\n"
         << "  \"residual_echo_likelihood\": "
         << JsonOptional(stats.residual_echo_likelihood) << ",\n"
         << "  \"residual_echo_likelihood_recent_max\": "
         << JsonOptional(stats.residual_echo_likelihood_recent_max) << ",\n"
         << "  \"sample_rate_hz\": 48000,\n"
         << "  \"schema_version\": 1,\n"
         << "  \"speaker_enable_authorized\": false,\n"
         << "  \"speaker_or_audiohub_called\": false,\n"
         << "  \"statistics_remote_tracks_assumed\": true,\n"
         << "  \"stream_delay_ms\": " << stream_delay_ms << ",\n"
         << "  \"tclw_claimed\": false,\n"
         << "  \"tuning_source\": \"" << profile.tuning_source << "\",\n"
         << "  \"webrtc_audio_processing_release\": \"2.1\",\n"
         << "  \"webrtc_upstream_basis\": \"M131\"\n"
         << "}\n";
  return stream.str();
}

}  // namespace

int main(int argc, char **argv) {
  std::filesystem::path output_path;
  std::filesystem::path report_path;
  bool output_created = false;
  try {
    const Options options = ParseOptions(argc, argv);
    output_path = options.output_wav;
    report_path = options.report;
    const WavData render = ParseWav(options.render_wav);
    const WavData capture = ParseWav(options.capture_wav);
    const AecProfile profile = DefaultAecProfile();
    if (render.samples.size() != capture.samples.size()) {
      throw std::runtime_error("render and capture WAV lengths differ");
    }

    webrtc::AudioProcessing::Config config;
    config.echo_canceller.enabled = true;
    config.echo_canceller.mobile_mode = false;
    config.echo_canceller.enforce_high_pass_filtering = false;
    config.high_pass_filter.enabled = false;
    config.noise_suppression.enabled = false;
    config.transient_suppression.enabled = false;
    config.gain_controller1.enabled = false;
    config.gain_controller2.enabled = false;
    rtc::scoped_refptr<webrtc::AudioProcessing> processing =
        webrtc::AudioProcessingBuilder().SetConfig(config).Create();
    if (processing == nullptr) {
      throw std::runtime_error("WebRTC AudioProcessing creation failed");
    }

    const webrtc::StreamConfig stream_config(
        static_cast<int>(kSampleRate), static_cast<std::size_t>(kChannels));
    std::array<std::int16_t, kSamplesPerFrame> render_output {};
    std::array<std::int16_t, kSamplesPerFrame> capture_output {};
    std::vector<std::int16_t> output_samples;
    output_samples.reserve(capture.samples.size());
    const auto started = std::chrono::steady_clock::now();
    const std::size_t frame_count = capture.samples.size() / kSamplesPerFrame;
    for (std::size_t frame = 0U; frame < frame_count; ++frame) {
      const std::int16_t *render_frame =
          render.samples.data() + frame * kSamplesPerFrame;
      const std::int16_t *capture_frame =
          capture.samples.data() + frame * kSamplesPerFrame;
      const int reverse_status = processing->ProcessReverseStream(
          render_frame, stream_config, stream_config, render_output.data());
      if (reverse_status != webrtc::AudioProcessing::kNoError) {
        throw std::runtime_error(
            "ProcessReverseStream failed with status " +
            std::to_string(reverse_status));
      }
      const int delay_status = processing->set_stream_delay_ms(options.stream_delay_ms);
      if (delay_status != webrtc::AudioProcessing::kNoError) {
        throw std::runtime_error(
            "set_stream_delay_ms failed with status " + std::to_string(delay_status));
      }
      const int capture_status = processing->ProcessStream(
          capture_frame, stream_config, stream_config, capture_output.data());
      if (capture_status != webrtc::AudioProcessing::kNoError) {
        throw std::runtime_error(
            "ProcessStream failed with status " + std::to_string(capture_status));
      }
      output_samples.insert(
          output_samples.end(), capture_output.begin(), capture_output.end());
    }
    const double processing_seconds = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - started).count();
    const webrtc::AudioProcessingStats stats = processing->GetStatistics(true);
    WriteWav(options.output_wav, output_samples);
    output_created = true;
    ExclusiveFile report(options.report);
    report.Write(BuildReport(
        frame_count, options.stream_delay_ms, processing_seconds, profile, stats));
    report.Flush();
    report.Commit();
    std::cout << "{\"aec_algorithm\":\"WebRTC AEC3\",\"frame_count\":"
              << frame_count << ",\"speaker_or_audiohub_called\":false}\n";
    return EXIT_SUCCESS;
  } catch (const std::exception &error) {
    if (output_created && !output_path.empty()) {
      ::unlink(output_path.c_str());
    }
    std::cerr << error.what() << '\n';
    return EXIT_FAILURE;
  }
}
