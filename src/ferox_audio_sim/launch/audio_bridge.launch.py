"""
ferox_audio_sim.audio_bridge — launch the host audio bridge.

Pushes the /ferox/<robot_id>/ namespace so the node's relative topics
resolve to:

    /ferox/<robot_id>/audio/mic_raw
    /ferox/<robot_id>/audio/speaker_out

Parameters come from config/audio_bridge.yaml. robot_id is exposed as a
launch arg and drives BOTH the namespace push and the node parameter, so
the two can never drift apart.
"""
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
        get_package_share_directory("ferox_audio_sim"),
        "config", "audio_bridge.yaml")

    args = [
        DeclareLaunchArgument(
            "robot_id", default_value="go2_01",
            description="Fleet ID. Becomes the /ferox/<robot_id>/ namespace."),
        DeclareLaunchArgument(
            "config_file", default_value=default_config,
            description="audio_bridge parameter YAML."),
    ]

    audio_bridge = Node(
        package="ferox_audio_sim",
        executable="audio_bridge",
        name="audio_bridge",
        output="screen",
        # robot_id passed after the YAML so the launch arg wins — keeps the
        # namespace and the node's own robot_id parameter in lock-step.
        parameters=[config_file, {"robot_id": robot_id}],
    )

    nav_group = GroupAction([
        PushRosNamespace(["/ferox/", robot_id]),
        audio_bridge,
    ])

    return LaunchDescription([*args, nav_group])
