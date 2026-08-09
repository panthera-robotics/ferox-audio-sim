from glob import glob
import os

from setuptools import setup


package_name = "ferox_audio_g1"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
        (os.path.join("share", package_name, "config"), glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Panthera Robotics",
    maintainer_email="engineering@panthera.robotics",
    description="Fail-closed Ferox speaker adapter for the Unitree G1 Voice API.",
    license="Proprietary",
    entry_points={
        "console_scripts": [
            "g1_voice_bridge = ferox_audio_g1.voice_bridge_node:main",
            "g1_voice_readonly_probe = ferox_audio_g1.readonly_probe:main",
            "g1_speaker_latency_probe = ferox_audio_g1.speaker_latency_probe:main",
            "g1_readaloud_probe = ferox_audio_g1.readaloud_probe:main",
            "audio_domain_gateway = ferox_audio_g1.audio_domain_gateway:main",
        ],
    },
)
