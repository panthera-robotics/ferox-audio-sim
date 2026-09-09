#include <rclcpp/rclcpp.hpp>
#include <unitree_go/msg/audio_data.hpp>

#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {

constexpr double kMinimumDurationSeconds = 5.0;
constexpr double kMaximumDurationSeconds = 120.0;

struct Options {
  double duration_seconds = 15.0;
  std::string reliability = "reliable";
  std::filesystem::path frames_output;
  std::filesystem::path metadata_output;
  std::string supervised_speaker_capture_token;
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

  void Write(const std::string &payload) {
    if (std::fwrite(payload.data(), 1U, payload.size(), handle_) != payload.size()) {
      throw std::runtime_error("short write: " + path_.string());
    }
  }

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
    if (argument == "--duration-s") {
      options.duration_seconds = std::stod(RequireValue(argc, argv, &index));
    } else if (argument == "--qos-reliability") {
      options.reliability = RequireValue(argc, argv, &index);
    } else if (argument == "--frames-output") {
      options.frames_output = RequireValue(argc, argv, &index);
    } else if (argument == "--metadata-output") {
      options.metadata_output = RequireValue(argc, argv, &index);
    } else if (argument == "--supervised-speaker-capture-token") {
      options.supervised_speaker_capture_token = RequireValue(argc, argv, &index);
    } else if (argument == "--ros-args") {
      break;
    } else {
      throw std::runtime_error("unknown argument: " + argument);
    }
  }
  if (!std::isfinite(options.duration_seconds) ||
      options.duration_seconds < kMinimumDurationSeconds ||
      options.duration_seconds > kMaximumDurationSeconds) {
    throw std::runtime_error("--duration-s must be finite and in [5, 120]");
  }
  if (options.reliability != "reliable" && options.reliability != "best_effort") {
    throw std::runtime_error("--qos-reliability must be reliable or best_effort");
  }
  if (options.frames_output.empty() || options.metadata_output.empty() ||
      std::filesystem::absolute(options.frames_output) ==
          std::filesystem::absolute(options.metadata_output)) {
    throw std::runtime_error("distinct --frames-output and --metadata-output are required");
  }
  if (options.supervised_speaker_capture_token.size() > 128U ||
      options.supervised_speaker_capture_token.find_first_not_of(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:+-") !=
        std::string::npos) {
    throw std::runtime_error(
      "--supervised-speaker-capture-token contains unsupported characters");
  }
  return options;
}

std::string ReadBootId() {
  std::ifstream input("/proc/sys/kernel/random/boot_id");
  std::string value;
  if (!input.good() || !std::getline(input, value) || value.size() != 36U ||
      value.find_first_not_of("0123456789abcdef-") != std::string::npos) {
    throw std::runtime_error("cannot read a canonical Linux boot_id");
  }
  return value;
}

std::string Base64(const std::vector<std::uint8_t> &bytes) {
  static constexpr char kAlphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string output;
  output.reserve(((bytes.size() + 2U) / 3U) * 4U);
  for (std::size_t index = 0; index < bytes.size(); index += 3U) {
    const std::uint32_t first = bytes[index];
    const std::uint32_t second = index + 1U < bytes.size() ? bytes[index + 1U] : 0U;
    const std::uint32_t third = index + 2U < bytes.size() ? bytes[index + 2U] : 0U;
    const std::uint32_t value = (first << 16U) | (second << 8U) | third;
    output.push_back(kAlphabet[(value >> 18U) & 0x3fU]);
    output.push_back(kAlphabet[(value >> 12U) & 0x3fU]);
    output.push_back(index + 1U < bytes.size() ? kAlphabet[(value >> 6U) & 0x3fU] : '=');
    output.push_back(index + 2U < bytes.size() ? kAlphabet[value & 0x3fU] : '=');
  }
  return output;
}

std::string PublisherGidHex(const rmw_gid_t &gid) {
  std::ostringstream stream;
  stream << std::hex << std::setfill('0');
  for (const std::uint8_t byte : gid.data) {
    stream << std::setw(2) << static_cast<unsigned int>(byte);
  }
  return stream.str();
}

std::int64_t SteadyNowNanoseconds() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

std::int64_t SystemNowNanoseconds() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::string FrameJson(
    const unitree_go::msg::AudioData &message, std::int64_t callback_steady_ns) {
  std::ostringstream stream;
  stream << std::fixed << std::setprecision(9)
         << "{\"payload_b64\":\"" << Base64(message.data)
         << "\",\"receive_steady_s\":"
         << static_cast<double>(callback_steady_ns) / 1'000'000'000.0
         << ",\"time_frame\":" << message.time_frame << "}\n";
  return stream.str();
}

std::string MetadataJson(
    const unitree_go::msg::AudioData &message,
    const rclcpp::MessageInfo &message_info,
    std::int64_t callback_steady_ns,
    std::int64_t callback_system_ns) {
  const auto &rmw = message_info.get_rmw_message_info();
  std::ostringstream stream;
  stream << "{\"callback_steady_ns\":" << callback_steady_ns
         << ",\"callback_system_ns\":" << callback_system_ns
         << ",\"from_intra_process\":"
         << (rmw.from_intra_process ? "true" : "false")
         << ",\"publisher_gid_hex\":\"" << PublisherGidHex(rmw.publisher_gid)
         << "\",\"record_type\":\"frame"
         << "\",\"rmw_received_timestamp_ns\":" << rmw.received_timestamp
         << ",\"rmw_source_timestamp_ns\":" << rmw.source_timestamp
         << ",\"time_frame\":" << message.time_frame << "}\n";
  return stream.str();
}

std::string MetadataHeaderJson(
    const Options &options,
    std::int64_t capture_start_steady_ns,
    std::int64_t capture_start_system_ns,
    const std::string &boot_id) {
  std::ostringstream stream;
  stream << std::fixed << std::setprecision(6)
         << "{\"capture_start_steady_ns\":" << capture_start_steady_ns
         << ",\"capture_start_system_ns\":" << capture_start_system_ns
         << ",\"host_boot_id\":\"" << boot_id << "\""
         << ",\"collector\":\"rclcpp_native\""
         << ",\"publisher_created\":false"
         << ",\"qos_reliability\":\"" << options.reliability << "\""
         << ",\"record_type\":\"capture_start\""
         << ",\"requested_duration_s\":" << options.duration_seconds
         << ",\"schema_version\":1"
         << ",\"source_topic\":\"/audiosender\""
         << ",\"speaker_or_audiohub_expected\":"
         << (options.supervised_speaker_capture_token.empty() ? "false" : "true")
         << ",\"supervised_speaker_capture_token\":";
  if (options.supervised_speaker_capture_token.empty()) {
    stream << "null";
  } else {
    stream << "\"" << options.supervised_speaker_capture_token << "\"";
  }
  stream << "}\n";
  return stream.str();
}

std::string MetadataTrailerJson(
    std::size_t frame_count,
    std::int64_t capture_start_steady_ns,
    std::int64_t capture_end_steady_ns,
    std::int64_t capture_end_system_ns,
    bool speaker_or_audiohub_expected) {
  std::ostringstream stream;
  stream << std::fixed << std::setprecision(9)
         << "{\"capture_end_steady_ns\":" << capture_end_steady_ns
         << ",\"capture_end_system_ns\":" << capture_end_system_ns
         << ",\"elapsed_s\":"
         << static_cast<double>(capture_end_steady_ns - capture_start_steady_ns) /
                1'000'000'000.0
         << ",\"frame_count\":" << frame_count
         << ",\"record_type\":\"capture_end\""
         << ",\"speaker_or_audiohub_called\":"
         << (speaker_or_audiohub_expected ? "true" : "false") << "}\n";
  return stream.str();
}

}  // namespace

int main(int argc, char **argv) {
  std::filesystem::path frames_path;
  std::filesystem::path metadata_path;
  try {
    const Options options = ParseOptions(argc, argv);
    frames_path = options.frames_output;
    metadata_path = options.metadata_output;
    ExclusiveFile frames_output(frames_path);
    ExclusiveFile metadata_output(metadata_path);

    rclcpp::init(argc, argv);
    const auto node = std::make_shared<rclcpp::Node>("go2_audio_native_timing_probe");
    auto qos = rclcpp::QoS(rclcpp::KeepLast(32));
    if (options.reliability == "reliable") {
      qos.reliable();
    } else {
      qos.best_effort();
    }
    std::size_t frame_count = 0U;
    const std::int64_t capture_start_steady_ns = SteadyNowNanoseconds();
    const std::int64_t capture_start_system_ns = SystemNowNanoseconds();
    const std::string host_boot_id = ReadBootId();
    metadata_output.Write(MetadataHeaderJson(
        options, capture_start_steady_ns, capture_start_system_ns, host_boot_id));
    const auto subscription = node->create_subscription<unitree_go::msg::AudioData>(
        "/audiosender", qos,
        [&](const unitree_go::msg::AudioData::ConstSharedPtr message,
            const rclcpp::MessageInfo &message_info) {
          const std::int64_t callback_steady_ns = SteadyNowNanoseconds();
          const std::int64_t callback_system_ns = SystemNowNanoseconds();
          frames_output.Write(FrameJson(*message, callback_steady_ns));
          metadata_output.Write(MetadataJson(
              *message, message_info, callback_steady_ns, callback_system_ns));
          ++frame_count;
        });
    (void)subscription;

    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::duration<double>(options.duration_seconds);
    rclcpp::executors::SingleThreadedExecutor executor;
    executor.add_node(node);
    while (rclcpp::ok() && std::chrono::steady_clock::now() < deadline) {
      executor.spin_some(std::chrono::milliseconds(10));
    }
    executor.remove_node(node);
    const std::int64_t capture_end_steady_ns = SteadyNowNanoseconds();
    const std::int64_t capture_end_system_ns = SystemNowNanoseconds();
    metadata_output.Write(MetadataTrailerJson(
        frame_count, capture_start_steady_ns, capture_end_steady_ns,
        capture_end_system_ns,
        !options.supervised_speaker_capture_token.empty()));
    frames_output.Flush();
    metadata_output.Flush();
    rclcpp::shutdown();
    if (frame_count == 0U) {
      throw std::runtime_error("no /audiosender frames were received");
    }
    frames_output.Commit();
    metadata_output.Commit();
    std::cout << "{\"collector\":\"rclcpp_native\",\"frame_count\":"
              << frame_count << ",\"publisher_created\":false,"
              << "\"speaker_or_audiohub_called\":"
              << (options.supervised_speaker_capture_token.empty() ? "false" : "true")
              << "}\n";
    return EXIT_SUCCESS;
  } catch (const std::exception &error) {
    if (rclcpp::ok()) {
      rclcpp::shutdown();
    }
    std::cerr << error.what() << '\n';
    return EXIT_FAILURE;
  }
}
