"""Source-bound conservative X6 eligibility, not OCR quality or acceptance.

Bounded axis-aligned painted rectangles, including nested Forms, in unrotated
MediaBox PDF user space (pdftoppm's default box). Rotation and UserUnit are
recorded separately; reported areas use squared PDF user coordinates, not
physical points or raster pixels. Unsupported geometry is unknown, never a
native exemption.
Text boxes/coverage are deliberately unavailable: any proven image suffices
under v1 rule 5, even with a complete invisible text overlay.
"""
from __future__ import annotations

import math
import unicodedata
from fractions import Fraction

POLICY = 'x6-eligibility-v1'
PROBE = 'pypdf-painted-rects-2'
MAX_OPERATIONS = 100000
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_IMAGES = 512
MAX_FORM_DEPTH = 32
MAX_FORMS = 512


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


# These operators cannot introduce a raster or change clipping by themselves.
# Patterns, Type3 fonts, soft masks and unknown operators remain unsupported.
_SAFE = set(b'BT ET Tm Td TD T* Tj TJ \' " Tc Tw Tz TL Ts '
            b'w J j M d ri i G g RG rg K k CS cs '
            b'S s f F f* B B* b b* MP DP BMC EMC BX EX sh'.split())
_PATH_END = {b'n', b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*'}


def _object(value):
    return value.get_object() if hasattr(value, 'get_object') else value


def _group(container):
    group = _object(container.get('/Group'))
    if group is None:
        return
    if (not isinstance(group, dict) or group.get('/S') != '/Transparency'
            or set(group) - {'/Type', '/S', '/CS', '/I', '/K'}
            or group.get('/CS') not in (None, '/DeviceGray', '/DeviceRGB', '/DeviceCMYK')):
        raise UnknownGeometry('unsupported_group')
    # Isolation/knockout and a device blending color space cannot introduce
    # raster placement or alter the clipping geometry. No coverage exemption
    # is inferred from opacity or compositing; any painted image still wins.


def _graphics_state(resources, operands):
    if len(operands) != 1:
        raise UnknownGeometry('operand_count')
    states = _object(resources.get('/ExtGState', {}))
    state = _object(states[operands[0]])
    neutral = {'/Type', '/LW', '/LC', '/LJ', '/ML', '/D', '/RI', '/OP', '/op',
               '/OPM', '/CA', '/ca', '/AIS', '/TK', '/BM', '/SMask'}
    if (not isinstance(state, dict) or set(state) - neutral
            or state.get('/SMask') not in (None, '/None')
            or state.get('/BM') not in (None, '/Normal', '/Compatible')):
        raise UnknownGeometry('unsupported_graphics_state')
    for key in ('/CA', '/ca'):
        if key in state:
            value, = _numbers([state[key]], 1)
            if not 0 <= value <= 1:
                raise UnknownGeometry('invalid_alpha')


class _Traversal:
    """One shared budget across repeated placements, forms and nested streams."""
    def __init__(self, evidence, pdf):
        self.evidence, self.pdf = evidence, pdf
        self.operations = self.bytes = self.forms = 0
        self.rectangles = []
        self.active = set()

    def walk(self, content, resources, matrix, clip, path_names=(), graphics_depth=0):
        from pypdf.generic import ContentStream
        if content is None:
            operations = []
        else:
            self.bytes += len(content.get_data())
            if self.bytes > MAX_STREAM_BYTES:
                raise UnknownGeometry('stream_bound')
            parsed = content if isinstance(content, ContentStream) else ContentStream(content, self.pdf)
            operations = parsed.operations
        resources = _object(resources)
        stack = []
        path, complex_path, pending_clip = [], False, False
        for index, (operands, operator) in enumerate(operations):
            self.operations += 1
            if self.operations > MAX_OPERATIONS:
                raise UnknownGeometry('operation_bound')
            if operator == b'q':
                if graphics_depth + len(stack) >= 64:
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
                fonts = _object(resources.get('/Font', {}))
                font = _object(fonts[operands[0]])
                if font.get('/Subtype') not in {'/Type1', '/MMType1', '/TrueType', '/Type0'}:
                    raise UnknownGeometry('unsupported_font_painting')
            elif operator == b'gs':
                _graphics_state(resources, operands)
            elif operator in {b'Do', b'INLINE IMAGE'}:
                if pending_clip:
                    raise UnknownGeometry('unapplied_clip')
                names = path_names + (str(operands[0]) if operator == b'Do' else 'inline',)
                if operator == b'Do':
                    xobjects = _object(resources.get('/XObject', {}))
                    obj = _object(xobjects[operands[0]])
                    if obj.get('/Subtype') == '/Form':
                        self.forms += 1
                        if len(names) > MAX_FORM_DEPTH or self.forms > MAX_FORMS:
                            raise UnknownGeometry('form_bound')
                        if id(obj) in self.active:
                            raise UnknownGeometry('form_cycle')
                        if obj.get('/FormType', 1) != 1 or any(k in obj for k in ('/OC', '/Ref', '/PS')):
                            raise UnknownGeometry('unsupported_form')
                        _group(obj)
                        form_matrix = _compose(matrix, obj.get('/Matrix', (1,0,0,1,0,0)))
                        box = _numbers(obj['/BBox'], 4)
                        if box[2] <= box[0] or box[3] <= box[1]:
                            raise UnknownGeometry('empty_form_domain')
                        form_clip = _intersect(clip, _rect(form_matrix,
                            (box[0], box[1], box[2]-box[0], box[3]-box[1])))
                        self.active.add(id(obj))
                        try:
                            self.walk(obj, obj.get('/Resources', resources), form_matrix, form_clip,
                                      names, graphics_depth + len(stack) + 1)
                        finally:
                            self.active.remove(id(obj))
                        continue
                    if obj.get('/Subtype') != '/Image' or any(k in obj for k in ('/SMask','/Mask','/ImageMask','/OC')):
                        raise UnknownGeometry('unsupported_xobject')
                else:
                    settings = operands.get('settings', {})
                    if any(k in settings for k in ('/IM', '/ImageMask', '/Mask', '/SMask')):
                        raise UnknownGeometry('unsupported_inline_mask')
                rect = _intersect(_rect(matrix, (0,0,1,1)), clip)
                if rect is not None:
                    if len(self.rectangles) >= MAX_IMAGES:
                        raise UnknownGeometry('image_bound')
                    self.rectangles.append(rect)
                    self.evidence['image_rectangles'].append([float(v) for v in rect])
                    self.evidence['image_rectangles_exact'].append([str(v) for v in rect])
                    self.evidence['image_placements'].append({'xobject_path': list(names), 'operation': index})
            elif operator not in _SAFE and operator not in _PATH_END:
                raise UnknownGeometry('unsupported_operator')
            if operator in _PATH_END:
                if pending_clip:
                    clip = _intersect(clip, path[0])
                path, complex_path, pending_clip = [], False, False
        if stack or pending_clip:
            raise UnknownGeometry('unbalanced_graphics_state')


def probe(page, source_sha256, number, text):
    """Evaluate a page already parsed from caller's hash-verified bytes.

    No IO, renderer, OCR, model or service calls. Measured image rectangles are
    evidence, not an assertion that their pixels contain words. Null text/U
    measurements explicitly mean unavailable, NOT zero or proof of coverage.
    """
    evidence: dict = dict(source_sha256=source_sha256, page=number, policy=POLICY,
                    probe=PROBE, numeric='exact-rational-placement-and-union; binary64-display-areas',
                    domain_convention='unrotated-MediaBox-pdftoppm-default', area_units='squared-PDF-user-coordinates',
                    domain=None, rotation=None, user_unit=None, probe_complete=False,
                    usable_text=usable_text(text), painted_image_present=None,
                    image_rectangles=[], image_rectangles_exact=[], image_placements=[], image_union_area=None, text_union_area=None,
                    uncovered_image_area=None, text_boxes_complete=False,
                    unavailable=['usable_text_boxes', 'uncovered_image_area'],
                    decision='unresolved', reason='page_eligibility_unresolved')
    images = evidence['image_rectangles']
    traversal = _Traversal(evidence, getattr(page, 'pdf', None))
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
        if page.get('/Annots'):
            raise UnknownGeometry('annotations')
        _group(page)
        traversal.walk(page.get_contents(), page.get('/Resources', {}), (1,0,0,1,0,0), domain)
        evidence['probe_complete'] = True
    except Exception as error:
        evidence['unavailable'].append(str(error) if isinstance(error, UnknownGeometry) else 'geometry_probe_failed')
    if images:
        evidence['painted_image_present'] = True
        # If the scan was interrupted the rectangles are only a lower bound;
        # no full union-area claim is made for incomplete traversal.
        if evidence['probe_complete']:
            try:
                area = float(_area(traversal.rectangles))
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
    return evidence
