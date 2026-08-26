from glob import glob
import os

from setuptools import setup


package_name = "ferox_audio_go2"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
        (os.path.join("share", package_name, "config"), glob("config/*.yaml")),
        (os.path.join("share", package_name, "evidence"), glob("evidence/*.json")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Panthera Robotics",
    maintainer_email="engineering@panthera.robotics",
    description="Evidence-gated Ferox audio adapter for Unitree Go2 hardware.",
    license="Proprietary",
    entry_points={"console_scripts": [
        "go2_audio_bridge = ferox_audio_go2.bridge_node:main",
        "go2_audio_readonly_discovery = ferox_audio_go2.discovery_probe:main",
        "go2_audio_decode_capture = ferox_audio_go2.decode_capture:main",
        "go2_audio_signal_metrics = ferox_audio_go2.signal_metrics:main",
        "go2_audio_aec_unavailable = ferox_audio_go2.aec_unavailable:main",
        "go2_audio_live_core_qualification = ferox_audio_go2.live_core_qualification:main",
        "go2_audio_transport_certificate = ferox_audio_go2.transport_certificate:main",
        "go2_audio_strict_timing = ferox_audio_go2.strict_timing:main",
        "go2_audio_hats_certificate = ferox_audio_go2.hats_certificate:main",
        "go2_audio_prepare_speaker_probe = ferox_audio_go2.speaker_probe:prepare_main",
        "go2_audio_speaker_probe = ferox_audio_go2.speaker_probe:main",
        "go2_audio_domain_gateway = ferox_audio_go2.domain_gateway:main",
    ]},
)
