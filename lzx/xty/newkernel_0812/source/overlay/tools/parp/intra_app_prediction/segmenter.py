"""Canonical multi-resolution logical-file segment mapping."""

from .schemas import SegmentSpec

RESOLUTIONS = (10, 100, 1000)


def _validate(file_page_count: int, requested_bins: int):
    if file_page_count <= 0:
        raise ValueError("empty files have no segments")
    if requested_bins <= 0:
        raise ValueError("requested_bins must be positive")


def segment_bounds(segment_id: int, file_page_count: int,
                   requested_bins: int):
    _validate(file_page_count, requested_bins)
    effective = min(requested_bins, file_page_count)
    if not 0 <= segment_id < effective:
        raise ValueError("segment_id outside effective bins")
    # Integer ceiling without floating point.  Python integers are unbounded,
    # so this remains exact for the full u64 input domain.
    start = (segment_id * file_page_count + effective - 1) // effective
    end = ((segment_id + 1) * file_page_count + effective - 1) // effective
    return start, end


def page_to_segment(page_index: int, file_page_count: int,
                    requested_bins: int, partition_generation: int = 1):
    _validate(file_page_count, requested_bins)
    if not 0 <= page_index < file_page_count:
        raise ValueError("page_index outside file")
    if partition_generation <= 0:
        raise ValueError("partition_generation must be positive")
    effective = min(requested_bins, file_page_count)
    segment_id = min(effective - 1,
                     page_index * effective // file_page_count)
    start, end = segment_bounds(segment_id, file_page_count,
                                requested_bins)
    return SegmentSpec(requested_bins, effective, segment_id, start, end,
                       file_page_count, partition_generation)


def all_segments(file_page_count: int, requested_bins: int,
                 partition_generation: int = 1):
    _validate(file_page_count, requested_bins)
    effective = min(requested_bins, file_page_count)
    return [SegmentSpec(requested_bins, effective, segment_id,
                        *segment_bounds(segment_id, file_page_count,
                                        requested_bins),
                        file_page_count, partition_generation)
            for segment_id in range(effective)]
