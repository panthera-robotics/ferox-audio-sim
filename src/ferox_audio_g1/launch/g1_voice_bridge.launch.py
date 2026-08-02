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
        get_package_share_directory("ferox_audio_g1"),
        "config",
        "g1_voice_bridge.yaml",
    )
    return LaunchDescription([
        DeclareLaunchArgument("robot_id", default_value="g1_01"),
        DeclareLaunchArgument("config_file", default_value=default_config),
        GroupAction([
            PushRosNamespace(["/ferox/", robot_id]),
            Node(
                package="ferox_audio_g1",
                executable="g1_voice_bridge",
                name="g1_voice_bridge",
                output="screen",
                parameters=[config_file],
            ),
        ]),
    ])
