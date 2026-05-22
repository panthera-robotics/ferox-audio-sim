# QoS contract: producers and consumers must use BEST_EFFORT reliability
# (rclpy.qos.qos_profile_sensor_data). RELIABLE subscribers will not match.
#
# Raw audio frame for inter-process audio transport.
# Producers fill exactly one of these per fixed-duration audio chunk
# (typically 100ms). Consumers must check sample_rate, channels, and
# sample_width before interpreting data — do not assume.
std_msgs/Header header

# Sample rate in Hz. 16000 is the canonical mic rate (Whisper-compatible);
# 22050 is the canonical Piper TTS output rate. Other values are legal.
uint32 sample_rate

# Channel count. 1 = mono, 2 = stereo interleaved. Production is mono.
uint8 channels

# Bytes per sample per channel. 2 = int16 (the only format the bridge
# emits in this iteration; float32 support is a future field-compatible
# extension).
uint8 sample_width

# Raw PCM samples, little-endian, interleaved if channels > 1.
# A 100ms chunk at 16kHz/mono/int16 = 3200 bytes.
uint8[] data
