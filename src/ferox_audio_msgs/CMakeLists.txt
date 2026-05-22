cmake_minimum_required(VERSION 3.8)
project(ferox_audio_msgs)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

# Audio transport interface. Kept in its own package so any repo
# (ferox-speech, ferox_audio_go2/_g1) can depend on just the message
# without pulling a heavier package in.
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/AudioChunk.msg"
  DEPENDENCIES std_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
