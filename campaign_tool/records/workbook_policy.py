"""Opt-in plain native XLSX qualification; no visual or semantic approval.

The pinned private accounting implementation remains on AgentOrange. Trusted
startup may load that exact read-only source there; records never choose code,
policy or paths. The loader verifies bytes, not owner authorization.
"""
import hashlib
import io
import re
import stat
import sys
import types
import zipfile
from pathlib import Path
from . import extraction_ledger as enrollment
from .intake import folder

POLICY = "plain-native-workbook-accounted-v1"
COMPONENT_SHA256 = "93b4b72dbd2868f9b6533ab20a5ae15b9eb801c2ea62db2ab2f5e9968b999723"  # pragma: allowlist secret - verified source-only component digest
MAX_PART = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_SHEETS = 1024
_TOKEN = object()


def require(value, reason):
    if not value:
        raise enrollment.ExtractionBindingError(reason)


class NativeWorkbookPolicy:
    def __init__(self, token, path, module):
        require(token is _TOKEN, "trusted_workbook_policy_loader_required")
        self._path, self._module = path, module

    @property
    def binding(self):
        require_component(self._path)
        require(enrollment.sha(enrollment.read_file(self._path, 65536)) == COMPONENT_SHA256,
                "workbook_component_changed")
        return {"policy": POLICY, "component_sha256": COMPONENT_SHA256,
                "scope_contract": "plain-workbook-native-rows-v3",
                "accounting_schema": "workbook-native-accounting-v1"}

    def qualify(self, raw, units):
        binding = self.binding
        require(type(raw) is bytes and type(units) is list, "workbook_input_shape")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                infos = package.infolist()
                require(0 < len(infos) <= MAX_SHEETS + 32 and len({x.filename for x in infos}) == len(infos),
                        "workbook_package_count_or_duplicates")
                require(sum(x.file_size for x in infos) <= MAX_TOTAL, "workbook_package_expansion_bound")
                for info in infos:
                    path = Path(info.filename)
                    require(not path.is_absolute() and '..' not in path.parts and '\\' not in info.filename
                            and not info.flag_bits & 1 and not stat.S_ISLNK(info.external_attr >> 16)
                            and not info.is_dir() and 0 <= info.file_size <= MAX_PART,
                            "workbook_package_member_unqualified")
                    # Bound every XML part before the private qualifier reads it.
                    if info.filename.endswith(('.xml','.rels')):
                        text = package.read(info).decode('utf-8-sig', errors='strict')
                        require('\x00' not in text and not re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I),
                                "workbook_xml_declaration_unqualified")
                inspected = self._module.inspect(package)
                accounted = self._module.account(package)
        except enrollment.ExtractionBindingError:
            raise
        except Exception:
            raise enrollment.ExtractionBindingError("workbook_package_or_component_failed") from None
        require(type(inspected) is dict and set(inspected) == {
            'contract','package_parts','scope','gaps','visual_review','semantic_acceptance'}
            and inspected['contract'] == binding['scope_contract']
            and inspected['scope'] == 'all_native_sheet_rows_only'
            and inspected['package_parts'] == len(infos) and inspected['gaps'] == []
            and inspected['semantic_acceptance'] is False and inspected['visual_review'] is False, "workbook_native_qualification_hold")
        require(type(accounted) is dict and set(accounted) == {
            'schema','denominator','sheets','gaps','coverage','sparse_grid_blanks_materialized',
            'formula_execution','visual_review','semantic_acceptance'}
            and accounted['schema'] == binding['accounting_schema'] and accounted['gaps'] == []
            and accounted['coverage'] == 'all_serialized_native_rows_and_cells'
            and accounted['sparse_grid_blanks_materialized'] is False
            and accounted['formula_execution'] is False and accounted['semantic_acceptance'] is False
            and accounted['visual_review'] is False,
            "workbook_accounting_unqualified")
        denominator = accounted['denominator']
        require(type(denominator) is dict and set(denominator) == {
            'declared_sheets','accounted_sheets','serialized_rows','serialized_cells','formula_cells'}
            and all(type(v) is int and v >= 0 for v in denominator.values())
            and 0 < denominator['declared_sheets'] == denominator['accounted_sheets'] <= MAX_SHEETS
            and denominator['serialized_rows'] <= 1048576 and denominator['serialized_cells'] <= 5000000
            and denominator['formula_cells'] == 0, "workbook_denominator_or_formula_hold")
        sheets = accounted['sheets']
        require(type(sheets) is list and len(sheets) == denominator['accounted_sheets'], "workbook_sheet_accounting")
        by_name = {}
        for sheet in sheets:
            require(type(sheet) is dict and type(sheet.get('name')) is str and 0 < len(sheet['name']) <= 31
                    and sheet['name'] not in by_name and sheet.get('state') in ('visible','hidden','veryHidden')
                    and all(type(sheet.get(key)) is int and sheet[key] >= 0 for key in (
                        'serialized_rows','serialized_cells','formula_cells','explicit_blank_cells'))
                    and sheet['formula_cells'] == sheet['explicit_blank_cells'] == 0,
                    "workbook_sheet_or_blank_disposition_hold")
            by_name[sheet['name']] = sheet
        require(sum(s['serialized_rows'] for s in sheets) == denominator['serialized_rows']
                and sum(s['serialized_cells'] for s in sheets) == denominator['serialized_cells'],
                "workbook_accounting_totals_mismatch")
        rows, cells, declared, positions = {n:0 for n in by_name}, {n:0 for n in by_name}, set(), set()
        for unit in units:
            require(type(unit) is dict and type(unit.get('locator')) is dict and type(unit.get('data')) is dict,
                    "workbook_unit_shape")
            name = unit['locator'].get('sheet')
            require(type(name) is str and bool(name) and name in by_name,
                    "workbook_unit_sheet_unbound")
            if unit.get('kind') == 'workbook_sheet':
                require(name not in declared and unit['data'].get('state') == by_name[name]['state'],
                        "workbook_sheet_unit_mismatch")
                declared.add(name)
            else:
                row = unit['locator'].get('row'); values = unit['data'].get('cells')
                require(unit.get('kind') == 'workbook_row' and type(row) is int and row > 0
                        and (name,row) not in positions and type(values) is list, "workbook_row_unit_mismatch")
                positions.add((name,row)); rows[name] += 1; cells[name] += len(values)
                require(unit.get('text') == folder.js(values), "workbook_row_text_mismatch")
                seen = set()
                for cell in values:
                    require(type(cell) is dict and set(cell) == {'cell','value','type'}
                            and type(cell['cell']) is str and cell['cell'] not in seen
                            and cell['value'] is not None and cell['type'] != 'f', "workbook_cell_unqualified")
                    match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', cell['cell'])
                    require(match is not None and int(match[2]) == row, "workbook_cell_locator_unbound")
                    seen.add(cell['cell'])
        require(declared == set(by_name) and all(rows[n] == by_name[n]['serialized_rows']
                and cells[n] == by_name[n]['serialized_cells'] for n in by_name),
                "workbook_serialized_denominator_mismatch")
        return {"binding": binding, "native_accounting": accounted,
                "visual_review_pending": True, "semantic_acceptance": False}


def require_component(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o400
            and info.st_nlink == 1, "workbook_component_not_readonly")


def load_native_workbook_policy(path):
    """Trusted startup only, on AgentOrange; no caller callback or record selector.

    Exact helper bytes are compiled locally, not exported or installed globally.
    Separate owner policy authorization remains a deployment gate.
    """
    path = enrollment.checked_path(path)
    require_component(path)
    raw = enrollment.read_file(path,65536)
    require(enrollment.sha(raw) == COMPONENT_SHA256, "workbook_component_pin_mismatch")
    name = 'campaign_tool.records._qualified_workbook_scope'
    module = types.ModuleType(name)
    module.__package__, module.__file__ = 'campaign_tool.records', str(path)
    module.__spec__ = None
    previous = sys.modules.get(name)
    try:
        sys.modules[name] = module
        exec(compile(raw,str(path),'exec'),module.__dict__)
        require(callable(getattr(module,'inspect',None)) and callable(getattr(module,'account',None)),
                "workbook_component_interface")
    except Exception:
        if previous is None:sys.modules.pop(name,None)
        else:sys.modules[name]=previous
        raise enrollment.ExtractionBindingError("workbook_component_load_failed") from None
    return NativeWorkbookPolicy(_TOKEN,path,module)
