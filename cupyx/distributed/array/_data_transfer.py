import dataclasses
from typing import Any, Iterable

import cupy
from cupy.cuda import Device, Event, Stream, get_current_stream

from cupy.cuda import nccl
if nccl.available:
    from cupy.cuda.nccl import NcclCommunicator     # type: ignore
else:
    class NcclCommunicator:     # type: ignore
        pass

from cupyx.distributed._nccl_comm import _get_nccl_dtype_and_count


@dataclasses.dataclass
class _AsyncData:
    data: cupy.ndarray
    ready: Event
    prevent_gc: Any = None      # TODO: Release it to avoid OOM

    def copy(self) -> '_AsyncData':
        with self.data.device:
            stream = get_current_stream()
            stream.wait_event(self.ready)
            update_data = self.data.copy()
            self.ready.record(stream)
            return _AsyncData(update_data, stream.record(), self.prevent_gc)


# Overwrite in replica mode, apply in op mode
_PartialUpdate = tuple[_AsyncData, tuple[slice, ...]]


if nccl.available:
    def _create_communicators(
        devices: Iterable[int],
    ) -> dict[int, NcclCommunicator]:
        comms_list = NcclCommunicator.initAll(list(devices))
        return {comm.device_id(): comm for comm in comms_list}

    def _transfer(
        src_comm: NcclCommunicator, src_stream: Stream, src_data: _AsyncData,
        dst_comm: NcclCommunicator, dst_stream: Stream, dst_dev: int,
    ) -> _AsyncData:
        src_dev = src_data.data.device.id
        if src_dev == dst_dev:
            return _AsyncData(src_data.data, src_data.ready)

        with Device(src_dev):
            src_stream.wait_event(src_data.ready)
            with src_stream:
                src_array = cupy.ascontiguousarray(src_data.data)
        with Device(dst_dev):
            with dst_stream:
                dst_buf = cupy.empty(src_array.shape, src_array.dtype)

        dtype, count = _get_nccl_dtype_and_count(src_array)
        nccl.groupStart()   # type: ignore

        with Device(src_dev):
            src_comm.send(src_array.data.ptr, count, dtype,
                          dst_comm.rank_id(), src_stream.ptr)

        with Device(dst_dev):
            dst_comm.recv(dst_buf.data.ptr, count, dtype,
                          src_comm.rank_id(), dst_stream.ptr)

            nccl.groupEnd()     # type: ignore
            return _AsyncData(dst_buf, dst_stream.record(),
                              prevent_gc=(src_data, src_array))
else:
    def _create_communicators(
        devices: Iterable[int],
    ) -> dict[int, NcclCommunicator]:
        return {dev: NcclCommunicator() for dev in devices}

    def _transfer(
        src_comm: NcclCommunicator, src_stream: Stream, src_data: _AsyncData,
        dst_comm: NcclCommunicator, dst_stream: Stream, dst_dev: int,
    ) -> _AsyncData:
        src_dev = src_data.data.device.id
        if src_dev == dst_dev:
            return _AsyncData(src_data.data, src_data.ready)

        with Device(dst_dev):
            dst_stream.wait_event(src_data.ready)
            with dst_stream:
                dst_data = src_data.data.copy()
            return _AsyncData(
                dst_data, dst_stream.record(), prevent_gc=src_data.data)
