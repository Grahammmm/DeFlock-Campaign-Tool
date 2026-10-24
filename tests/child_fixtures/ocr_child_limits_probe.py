"""Resource-limit probe for tests/records/test_ocr.py
(OCRTests.test_child_limits_apply_address_space_and_file_size).

Takes no arguments; prints the soft RLIMIT_AS and RLIMIT_FSIZE values.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    import resource;print(resource.getrlimit(resource.RLIMIT_AS)[0],resource.getrlimit(resource.RLIMIT_FSIZE)[0])
