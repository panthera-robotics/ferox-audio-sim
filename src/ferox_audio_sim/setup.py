from glob import glob
import os
from setuptools import setup

package_name = 'ferox_audio_sim'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages',
         ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'),
         glob('launch/*.launch.py')),
        (os.path.join('share', package_name, 'config'),
         glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Mohammed Magdy',
    maintainer_email='mohammed@pantherarobots.com',
    description='Host-side audio bridge: mic/speaker <-> ROS audio topics.',
    license='Proprietary',
    entry_points={
        'console_scripts': [
            'audio_bridge = ferox_audio_sim.audio_bridge_node:main',
        ],
    },
)
