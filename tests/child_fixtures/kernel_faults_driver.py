"""Driver child for tests/records/test_controller_kernel_faults.py
(ControllerKernelFaultTests.spawn).

Arguments arrive in sys.argv order: profile path, action, invocation, job id.
"""
import sys
if not sys.flags.isolated: sys.path[0] = ''


if __name__ == "__main__":
    from tests.records.test_controller_kernel_faults import _child; import sys; _child(*sys.argv[1:])
