"""Synthetic, minimal native OLE message fixtures; no received records."""
import io
import struct


def available():
    try:
        import extract_msg
        import olefile
    except ImportError:
        return False
    return True


def message(*, attachment=None, embedded=False):
    from extract_msg.ole_writer import OleWriter
    writer = OleWriter()

    def fixed(tag, value):
        return struct.pack('<IIII', tag, 6, value, 0)

    def stream(prefix, tag, payload):
        writer.addEntry(prefix + ['__substg1.0_%08X' % tag], payload)
        return struct.pack('<IIII', tag, 6, len(payload), 0)

    def text(prefix, tag, value):
        return stream(prefix, tag, value.encode('utf-16-le'))

    def build(prefix, is_embedded, child):
        count = int(child is not None or (embedded and not is_embedded))
        props = struct.pack('<8xIIII', 0, count, 0, count)
        if not is_embedded:
            props += b'\0' * 8
        props += fixed(0x340D0003, 0x40000)
        props += text(prefix, 0x001A001F, 'IPM.Note')
        props += text(prefix, 0x0037001F, 'Synthetic embedded' if is_embedded else 'Synthetic outer')
        props += text(prefix, 0x1000001F, 'Synthetic native body')
        writer.addEntry(prefix + ['__properties_version1.0'], props)
        if not count:
            return
        ap = prefix + ['__attach_version1.0_#00000000']
        writer.addEntry(ap, storage=True)
        native = embedded and not is_embedded
        att = b'\0' * 8 + fixed(0x37050003, 5 if native else 1)
        att += text(ap, 0x3707001F, 'embedded.msg' if native else 'nested.eml')
        if native:
            mp = ap + ['__substg1.0_3701000D']
            writer.addEntry(mp, storage=True)
            build(mp, True, child)
        else:
            att += stream(ap, 0x37010102, child)
        writer.addEntry(ap + ['__properties_version1.0'], att)

    # MSG named properties are shared at the root, including embedded messages.
    # Empty, present streams describe a valid message with no named properties.
    writer.addEntry(['__nameid_version1.0'], storage=True)
    for tag in ('00020102', '00030102', '00040102'):
        writer.addEntry(['__nameid_version1.0', '__substg1.0_' + tag], b'')
    build([], False, attachment)

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
