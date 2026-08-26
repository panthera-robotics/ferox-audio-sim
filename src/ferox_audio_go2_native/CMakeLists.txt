cmake_minimum_required(VERSION 3.8)
project(ferox_audio_go2_native)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(unitree_go REQUIRED)

add_executable(go2_audio_native_timing_probe src/native_timing_probe.cpp)
ament_target_dependencies(go2_audio_native_timing_probe rclcpp unitree_go)
target_compile_options(go2_audio_native_timing_probe PRIVATE
  -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(WEBRTC_APM QUIET IMPORTED_TARGET webrtc-audio-processing-2)
endif()
if(TARGET PkgConfig::WEBRTC_APM)
  pkg_get_variable(WEBRTC_APM_PREFIX webrtc-audio-processing-2 prefix)
  if(NOT WEBRTC_APM_PREFIX)
    message(FATAL_ERROR "webrtc-audio-processing-2 pkg-config prefix is missing")
  endif()
  add_executable(go2_audio_aec3_offline src/aec3_offline.cpp)
  target_link_libraries(go2_audio_aec3_offline PRIVATE PkgConfig::WEBRTC_APM)
  # The 2.1 Meson fallback installs its bundled Abseil headers at
  # <prefix>/include/absl but does not list that root in the generated .pc file.
  target_include_directories(go2_audio_aec3_offline PRIVATE
    "${WEBRTC_APM_PREFIX}/include")
  target_compile_options(go2_audio_aec3_offline PRIVATE
    -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion)
  install(TARGETS go2_audio_aec3_offline DESTINATION lib/${PROJECT_NAME})
else()
  message(STATUS "webrtc-audio-processing-2 not found; AEC3 offline tool omitted")
endif()

install(TARGETS go2_audio_native_timing_probe DESTINATION lib/${PROJECT_NAME})
ament_package()
