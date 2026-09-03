"""Small, deterministic streaming PCM resampler for the G1 voice boundary.

Ferox speech emits 22.05 kHz PCM, while the public G1 ``PlayStream``
interface consumes 16 kHz PCM.  This module deliberately has no NumPy,
Torch, or ROS dependency: it is used in the small ARM64 robot adapter and
must remain bounded and reproducible on the Jetson image.
"""
from __future__ import annotations

from array import array
import sys


class PcmResampleError(ValueError):
    """The PCM stream cannot be converted safely."""


class LinearPcmResampler:
    """Stateful linear-interpolation resampler for mono signed 16-bit PCM.

    Output sample positions are defined against the complete source stream,
    so feeding a stream in different chunk boundaries produces the same bytes
    as feeding it in one piece.  A final chunk uses the last source sample for
    the endpoint, giving exactly ``round(input_samples * target/source)``
    output samples.
    """

    def __init__(self, source_rate: int, target_rate: int) -> None:
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        if self.source_rate <= 0 or self.target_rate <= 0:
            raise ValueError("PCM sample rates must be positive")
        self.reset()

    def reset(self) -> None:
        self._samples: list[int] = []
        self._base_index = 0
        self._input_samples = 0
        self._emitted_samples = 0
        self._ended = False

    @property
    def input_samples(self) -> int:
        return self._input_samples

    @property
    def output_samples(self) -> int:
        return self._emitted_samples

    def feed(self, data: bytes, *, end: bool = False) -> bytes:
        if self._ended:
            raise PcmResampleError("PCM data arrived after the end of the stream")
        payload = bytes(data)
        if len(payload) % 2:
            raise PcmResampleError("PCM chunk ends in a partial int16 sample")
        if payload:
            values = array("h")
            values.frombytes(payload)
            if sys.byteorder != "little":
                values.byteswap()
            self._samples.extend(int(value) for value in values)
            self._input_samples += len(values)
        if end:
            self._ended = True

        if not self._input_samples:
            if end:
                raise PcmResampleError("cannot finish an empty PCM stream")
            return b""

        desired = self._rounded_output_count() if end else (
            self._input_samples * self.target_rate // self.source_rate)
        output: list[int] = []
        while self._emitted_samples < desired:
            position_numerator = self._emitted_samples * self.source_rate
            left_index, fraction = divmod(position_numerator, self.target_rate)
            if left_index < self._base_index:
                raise PcmResampleError("resampler chronology state was lost")
            local_index = left_index - self._base_index
            if local_index >= len(self._samples):
                break
            if local_index + 1 < len(self._samples):
                left = self._samples[local_index]
                right = self._samples[local_index + 1]
            elif end:
                left = right = self._samples[local_index]
            else:
                # Keep the last source sample until the next chunk supplies
                # the neighbour needed for interpolation.
                break
            delta = right - left
            if fraction:
                value_numerator = left * self.target_rate + delta * fraction
                value = self._round_ratio(value_numerator, self.target_rate)
            else:
                value = left
            output.append(max(-32768, min(32767, value)))
            self._emitted_samples += 1

        self._discard_consumed_prefix()
        packed = array("h", output)
        if sys.byteorder != "little":
            packed.byteswap()
        return packed.tobytes()

    def _rounded_output_count(self) -> int:
        numerator = self._input_samples * self.target_rate
        return (numerator + self.source_rate // 2) // self.source_rate

    @staticmethod
    def _round_ratio(numerator: int, denominator: int) -> int:
        if numerator >= 0:
            return (numerator + denominator // 2) // denominator
        return -((-numerator + denominator // 2) // denominator)

    def _discard_consumed_prefix(self) -> None:
        if self._emitted_samples >= self._rounded_output_count():
            return
        next_position = self._emitted_samples * self.source_rate
        next_left, _ = divmod(next_position, self.target_rate)
        drop = max(0, next_left - self._base_index)
        if drop:
            del self._samples[:drop]
            self._base_index += drop


def resample_s16le(data: bytes, *, source_rate: int, target_rate: int) -> bytes:
    """Resample one complete S16LE stream in a deterministic one-shot call."""
    return LinearPcmResampler(source_rate, target_rate).feed(data, end=True)
