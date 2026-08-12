import unittest

from region_decode import decode_trace_line


class Phase27TraceTests(unittest.TestCase):
    LINE = (
        " worker [000] 123.0: parp_region_evidence: "
        "sample=9 sample_time=122000 pid=10 tgid=10 domain=22 app=1 "
        "bind_generation=3 foreground_epoch=4 model=19 mm_cookie=5 "
        "type=0 align=0 region_start=4096 region_end=8192 logical_start=7 "
        "nr_pages=1 dev_major=8 dev_minor=5 inode=99 file_version=6 "
        "file_size=4097 file_pages=2 vma_signature=0 sample_us=5000 "
        "aggregation_us=1000000 access_evidence=2 age=3 "
        "confidence_q15=32767 reasons=0x0")

    def test_extended_file_identity_decodes(self):
        row = decode_trace_line(self.LINE)
        self.assertEqual((row["dev_major"], row["dev_minor"], row["inode"],
                          row["file_version"]), (8, 5, 99, 6))

    def test_file_size_and_page_count_decode(self):
        row = decode_trace_line(self.LINE)
        self.assertEqual((row["file_size_bytes"], row["file_page_count"]),
                         (4097, 2))

    def test_context_and_intervals_decode(self):
        row = decode_trace_line(self.LINE)
        self.assertEqual((row["bind_generation"], row["foreground_epoch"],
                          row["model_version"], row["sample_interval_us"],
                          row["aggregation_interval_us"]),
                         (3, 4, 19, 5000, 1000000))

    def test_region_bounds_and_sample_time_decode(self):
        row = decode_trace_line(self.LINE)
        self.assertEqual((row["sample_timestamp_ns"], row["region_start"],
                          row["region_end"]), (122000, 4096, 8192))


if __name__ == "__main__":
    unittest.main()
