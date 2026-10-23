"""Source-bound conservative X6 eligibility, not OCR quality or acceptance.

Phase 1: bounded axis-aligned painted rectangles in unrotated MediaBox PDF
user space (pdftoppm's default box). Rotation does not change intersections
in that space. Unsupported geometry is unknown, never a native exemption.
Text boxes/coverage are deliberately unavailable: any proven image suffices
under v1 rule 5, even with a complete invisible text overlay.
"""
from __future__ import annotations

import math
import unicodedata
from fractions import Fraction

POLICY = 'x6-eligibility-v1'
PROBE = 'pypdf-painted-rects-1'
MAX_OPERATIONS = 100000
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_IMAGES = 512


def usable_text(text):
    """Predicate only; callers preserve raw text, including non-Latin/symbols."""
    return any(not c.isspace() and c != '\ufffd'
               and unicodedata.category(c) not in {'Cc', 'Cf', 'Cs', 'Cn'} for c in text)


class UnknownGeometry(ValueError):
    pass


def _numbers(values, count):
    if len(values) != count:
        raise UnknownGeometry('operand_count')
    result = tuple(Fraction(str(v)) for v in values)
    if not all(math.isfinite(float(v)) and max(v.numerator.bit_length(), v.denominator.bit_length()) <= 4096 for v in result):
        raise UnknownGeometry('nonfinite_geometry')
    return result


def _intersect(a, b):
    if a is None or b is None:
        return None
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _rect(matrix, values):
    x, y, width, height = _numbers(values, 4)
    a, b, c, d, e, f = matrix
    # Exact axis alignment only; no rounded sliver/rotated-bounding-box claims.
    if not ((b == c == 0 and a != 0 and d != 0) or (a == d == 0 and b != 0 and c != 0)):
        raise UnknownGeometry('non_axis_transform')
    points = [(a*u+c*v+e, b*u+d*v+f) for u, v in
              ((x,y), (x+width,y), (x,y+height), (x+width,y+height))]
    if not all(math.isfinite(v) for point in points for v in point):
        raise UnknownGeometry('nonfinite_transform')
    xs, ys = zip(*points)
    if min(xs) == max(xs) or min(ys) == max(ys):
        raise UnknownGeometry('degenerate_or_numerically_collapsed_rectangle')
    return min(xs), min(ys), max(xs), max(ys)


def _compose(current, incoming):
    a,b,c,d,e,f = current
    g,h,i,j,k,l = _numbers(incoming, 6)
    result = (a*g+c*h, b*g+d*h, a*i+c*j, b*i+d*j, a*k+c*l+e, b*k+d*l+f)
    if not all(math.isfinite(float(v)) and max(v.numerator.bit_length(), v.denominator.bit_length()) <= 4096 for v in result):
        raise UnknownGeometry('nonfinite_transform')
    return result


def _area(rectangles):
    """Exact rational union over recorded rectangles; no thresholds."""
    xs = sorted({r[i] for r in rectangles for i in (0,2)})
    area = 0
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((r[1],r[3]) for r in rectangles if r[0] < right and r[2] > left)
        end, height = None, 0
        for low, high in intervals:
            height += max(0, high-(low if end is None else max(low,end)))
            end = high if end is None else max(end,high)
        area += (right-left)*height
    return area


# These operators cannot paint a raster or change the clipping domain by
# themselves. Pattern color, external graphics states, forms and text clipping
# are NOT assumed safe. They require phase-2 qualification or remain unknown.
_SAFE = set(b'BT ET Tm Td TD T* Tj TJ \' " Tc Tw Tz TL Ts '
            b'w J j M d ri i G g RG rg K k CS cs '
            b'S s f F f* B B* b b* MP DP BMC EMC BX EX sh'.split())
_PATH_END = {b'n', b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*'}


def probe(page, source_sha256, number, text):
    """Evaluate a page already parsed from caller's hash-verified bytes.

    No IO, renderer, OCR, model or service calls. Measured image rectangles are
    evidence, not an assertion that their pixels contain words. Null text/U
    measurements explicitly mean unavailable, NOT zero or proof of coverage.
    """
    evidence: dict = dict(source_sha256=source_sha256, page=number, policy=POLICY,
                    probe=PROBE, numeric='exact-rational-placement-and-union; binary64-display-areas',
                    domain_convention='unrotated-MediaBox-pdftoppm-default',
                    domain=None, rotation=None, user_unit=None, probe_complete=False,
                    usable_text=usable_text(text), painted_image_present=None,
                    image_rectangles=[], image_rectangles_exact=[], image_union_area=None, text_union_area=None,
                    uncovered_image_area=None, text_boxes_complete=False,
                    unavailable=['usable_text_boxes', 'uncovered_image_area'],
                    decision='unresolved', reason='page_eligibility_unresolved')
    images = evidence['image_rectangles']
    exact_images = []
    try:
        domain = _numbers(page.mediabox, 4)
        if domain[2] <= domain[0] or domain[3] <= domain[1]:
            raise UnknownGeometry('empty_domain')
        unit = float(page.get('/UserUnit', 1))
        raw_rotation = Fraction(str(page.get('/Rotate', 0)))
        rotation = int(raw_rotation)
        if not math.isfinite(unit) or unit <= 0 or rotation != raw_rotation or rotation % 90:
            raise UnknownGeometry('page_transform')
        evidence.update(domain=[float(v) for v in domain], rotation=rotation, user_unit=unit)
        if page.get('/Annots') or page.get('/Group'):
            raise UnknownGeometry('annotation_or_group')
        content = page.get_contents()
        if content is None:
            operations = []
        else:
            if len(content.get_data()) > MAX_STREAM_BYTES:
                raise UnknownGeometry('stream_bound')
            operations = content.operations
        if len(operations) > MAX_OPERATIONS:
            raise UnknownGeometry('operation_bound')
        matrix, clip, stack = (1,0,0,1,0,0), domain, []
        path, complex_path, pending_clip = [], False, False
        resources = page.get('/Resources', {})
        resources = resources.get_object() if hasattr(resources, 'get_object') else resources
        for operands, operator in operations:
            if operator == b'q':
                if len(stack) >= 64:
                    raise UnknownGeometry('stack_bound')
                stack.append((matrix, clip))
            elif operator == b'Q':
                if not stack:
                    raise UnknownGeometry('unbalanced_graphics_state')
                matrix, clip = stack.pop()
            elif operator == b'cm':
                matrix = _compose(matrix, operands)
            elif operator == b're':
                path.append(_rect(matrix, operands))
            elif operator in {b'm', b'l', b'c', b'v', b'y', b'h'}:
                complex_path = True
            elif operator in {b'W', b'W*'}:
                if complex_path or len(path) != 1:
                    raise UnknownGeometry('unsupported_clip')
                pending_clip = True
            elif operator == b'Tr':
                mode, = _numbers(operands, 1)
                if mode not in (0,1,2,3):
                    raise UnknownGeometry('text_clip')
            elif operator == b'Tf':
                fonts = resources.get('/Font', {})
                fonts = fonts.get_object() if hasattr(fonts, 'get_object') else fonts
                font = fonts[operands[0]].get_object()
                if font.get('/Subtype') not in {'/Type1', '/MMType1', '/TrueType', '/Type0'}:
                    raise UnknownGeometry('unsupported_font_painting')
            elif operator in {b'Do', b'INLINE IMAGE'}:
                if operator == b'Do':
                    xobjects = resources.get('/XObject', {})
                    xobjects = xobjects.get_object() if hasattr(xobjects, 'get_object') else xobjects
                    obj = xobjects[operands[0]].get_object()
                    if obj.get('/Subtype') != '/Image' or any(k in obj for k in ('/SMask','/Mask','/ImageMask','/OC')):
                        raise UnknownGeometry('unsupported_xobject')
                if pending_clip:
                    raise UnknownGeometry('unapplied_clip')
                rect = _intersect(_rect(matrix, (0,0,1,1)), clip)
                if rect is not None:
                    if len(images) >= MAX_IMAGES:
                        raise UnknownGeometry('image_bound')
                    exact_images.append(rect)
                    images.append([float(v) for v in rect])
                    evidence['image_rectangles_exact'].append([str(v) for v in rect])
            elif operator not in _SAFE and operator not in _PATH_END:
                raise UnknownGeometry('unsupported_operator')
            if operator in _PATH_END:
                if pending_clip:
                    clip = _intersect(clip, path[0])
                path, complex_path, pending_clip = [], False, False
        if stack or pending_clip:
            raise UnknownGeometry('unbalanced_graphics_state')
        evidence['probe_complete'] = True
    except Exception as error:
        evidence['unavailable'].append(str(error) if isinstance(error, UnknownGeometry) else 'geometry_probe_failed')
    if images:
        evidence['painted_image_present'] = True
        # If the scan was interrupted the rectangles are only a lower bound;
        # no full union-area claim is made for incomplete traversal.
        if evidence['probe_complete']:
            try:
                area = float(_area(exact_images))
                if area <= 0 or not math.isfinite(area):
                    raise UnknownGeometry('nonpositive_measured_area')
                evidence['image_union_area'] = area
            except (UnknownGeometry, OverflowError) as error:
                evidence['probe_complete'] = False
                evidence['unavailable'].append(str(error))
        evidence.update(decision='ocr_required', reason='painted_raster_image')
    elif evidence['probe_complete']:
        evidence.update(painted_image_present=False, image_union_area=0.0)
        evidence.update(decision='native_sufficient' if evidence['usable_text'] else 'ocr_required',
                        reason='no_painted_raster' if evidence['usable_text'] else 'no_usable_native_text')
    # No text is sufficient evidence of needing OCR, but unknown rendering
    # semantics still block; native admission never becomes ok on uncertainty.
    return evidence
