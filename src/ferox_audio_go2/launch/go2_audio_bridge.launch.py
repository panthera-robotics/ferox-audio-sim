import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, PushRosNamespace


def generate_launch_description():
    robot_id = LaunchConfiguration("robot_id")
    config_file = LaunchConfiguration("config_file")
    default_config = os.path.join(
        get_package_share_directory("ferox_audio_go2"),
        "config", "go2_audio_bridge.yaml")
    overrides = {
        "mic_enabled": LaunchConfiguration("mic_enabled"),
        "speaker_enabled": LaunchConfiguration("speaker_enabled"),
        "hardware_profile": LaunchConfiguration("hardware_profile"),
        "runtime_firmware": LaunchConfiguration("runtime_firmware"),
        "evidence_path": LaunchConfiguration("evidence_path"),
        "evidence_sha256": LaunchConfiguration("evidence_sha256"),
    }
    declarations = [
        DeclareLaunchArgument("robot_id", default_value="go2_02"),
        DeclareLaunchArgument("config_file", default_value=default_config),
        DeclareLaunchArgument("mic_enabled", default_value="false"),
        DeclareLaunchArgument("speaker_enabled", default_value="false"),
        DeclareLaunchArgument("hardware_profile", default_value="disabled"),
        DeclareLaunchArgument("runtime_firmware", default_value="disabled"),
        DeclareLaunchArgument("evidence_path", default_value="disabled"),
        DeclareLaunchArgument("evidence_sha256", default_value="disabled"),
    ]
    return LaunchDescription([
        *declarations,
        GroupAction([
            PushRosNamespace(["/ferox/", robot_id]),
            Node(
                package="ferox_audio_go2",
                executable="go2_audio_bridge",
                name="go2_audio_bridge",
                output="screen",
                parameters=[config_file, {"robot_id": robot_id, **overrides}],
            ),
        ]),
    ])
