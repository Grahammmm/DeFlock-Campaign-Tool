"""Bounded extraction routes and page accounting around the existing intake worker."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import signal
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from .intake import folder
from .extract import ocr as page_ocr

VERSION = 'format-routes-1'
SUPPORTED = {'pdf','eml','csv','tsv','docx','xlsx','zip','txt','md','json','jsonl','xml','html','htm','log','rst','yaml','yml'}
IMAGES = {'png','jpg','jpeg','gif','tif','tiff','webp','bmp'}
MAX_UNITS = 100000
MAX_UNIT_BYTES = 64*1024*1024
OCR_METHOD = page_ocr.VERSION
OCR_TEXT_STATES = {'ocr_text_unreviewed','visual_check_queued'}

def route(form, role='original'):
    form=str(form).lower()
    if role in {'inline_branding','signature_image','duplicate_delivery'}:
        return {'route':'low-value-proposed','owner':'catalog','review_required':True}
    if form in IMAGES:
        return {'route':'image-direct-review','owner':'visual-review','review_required':True}
    if form == 'msg':
        return {'route':'decoder:msg','owner':'runtime','review_required':True}
    if form in SUPPORTED:
        return {'route':'extract:'+form,'owner':'runner','review_required':True}
    return {'route':'decoder-needed:'+form,'owner':'runtime','review_required':True}

def inventory(rows):
    if len(rows)>100000:raise ValueError('inventory bound')
    return [dict(original_sha256=r['sha256'],format=r.get('format','unknown'),**route(r.get('format','unknown'),r.get('role','original'))) for r in rows if r.get('extraction_status')!='complete']

def _version(name):
    try:return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:return 'unavailable'

def _private_directory(root):
    root=Path(root)
    if root.resolve()!=root or not root.is_dir():raise ValueError('output root must be a real directory')
    mode=root.stat()
    if mode.st_uid!=os.getuid() or stat.S_IMODE(mode.st_mode)&0o077:raise ValueError('private output root required')
    return root

def _capture(source, expected):
    with folder.secure_open(source) as stream:
        before=os.fstat(stream.fileno())
        if before.st_size>folder.LIMITS['source_bytes']:raise ValueError('source_size_limit')
        raw=stream.read(folder.LIMITS['source_bytes']+1);after=os.fstat(stream.fileno())
    if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('source_changed')
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('source_hash_mismatch')
    return raw

def _write(path, value):
    data=(json.dumps(value,sort_keys=True,ensure_ascii=True)+'\n').encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(data)
    return hashlib.sha256(data).hexdigest()

def _image(source,raw):
    import io
    from PIL import Image
    Image.MAX_IMAGE_PIXELS=40000000
    units=[];total_pixels=0
    with Image.open(io.BytesIO(raw)) as image:
        frames=getattr(image,'n_frames',1);form=image.format
        if frames>1000:raise ValueError('image_resource_limit')
        for frame in range(frames):
            image.seek(frame);width,height=image.size
            total_pixels+=width*height
            if width*height>40000000 or total_pixels>160000000:raise ValueError('image_resource_limit')
            image.load()
            units.append({'kind':'image','locator':{'image':1,'frame':frame+1},'text':'','data':{'width':width,'height':height,'frames':frames,'format':form,'visual_review':'not_done'}})
    return units, 'pillow', _version('Pillow')

def _msg(source,raw,root):
    from .intake import mail_wire
    plan=mail_wire.prepare(raw,"msg",max_total_bytes=min(folder.LIMITS["expanded_bytes"],512*1024**2),
                           max_parts=min(1000,folder.LIMITS["members"]),
                           max_captures=min(100,folder.LIMITS["members"]),max_depth=min(32,folder.LIMITS["depth"]))
    mail_wire.store(plan,root)
    mail_wire.store_native(plan,root)
    children=[{"sha":hashlib.sha256(c.payload).hexdigest(),"bytes":len(c.payload),
               "name":c.metadata.get("original_filename") or "attachment.bin",
               "locator":{"part":c.part,"wire_schema":mail_wire.SCHEMA},
               "relation":c.metadata["relation"],"parent_sha":c.parent_sha256,
               "format":c.metadata.get("format") or folder.fmt(c.metadata.get("original_filename") or "",c.payload[:16384],c.payload)}
              for c in plan.captures]
    return list(mail_wire.project_units(plan)),"extract-msg",_version("extract-msg"),children

def page_manifest(units, expected_pages=None, parser='unknown', parser_version='unknown', ocr_receipts=None):
    """Page rows for coverage accounting.

    ``ocr_receipts`` optionally maps page number -> verified per-page OCR receipt summary
    (receipt_id, receipt_sha256, status, blocked_reason, confidence, method_version).
    Such pages keep ``needs_visual_review`` and never count as ``ok``; the denominator is
    unchanged because receipts only annotate rows that already exist.
    """
    rows=[];seen=set()
    for unit in units:
        loc=unit.get('locator',{});page=loc.get('page')
        if page is None:continue
        if type(page)is not int or page<1 or page in seen:raise ValueError('invalid_or_duplicate_page_locator')
        seen.add(page);data=unit.get('data',{});text=unit.get('text','')
        blocked=bool(data.get('error'));needs=bool(data.get('ocr_needed')) or not str(text).strip()
        rows.append({'page_no':page,'method':parser,'method_version':parser_version,
                     'derivative_sha256':hashlib.sha256(str(text).encode()).hexdigest(),
                     'confidence':None,'needs_visual_review':True,
                     'status':'blocked' if blocked else 'partial' if needs else 'ok',
                     'reason':'page_error' if blocked else 'ocr_needed_or_blank' if needs else None})
    if expected_pages is not None:
        if type(expected_pages)is not int or expected_pages<0 or expected_pages>100000:raise ValueError('page_bound')
        if any(p>expected_pages for p in seen):raise ValueError('page_outside_denominator')
        for page in range(1,expected_pages+1):
            if page not in seen:rows.append({'page_no':page,'method':parser,'method_version':parser_version,'derivative_sha256':None,'confidence':None,'needs_visual_review':True,'status':'blocked','reason':'page_not_extracted'})
    rows=sorted(rows,key=lambda x:x['page_no'])
    if ocr_receipts:
        by_page={row['page_no']:row for row in rows}
        for page,receipt in sorted(ocr_receipts.items()):
            row=by_page.get(page)
            if row is None:raise ValueError('ocr_receipt_page_outside_denominator')
            state=receipt['status']
            row.update(method=OCR_METHOD,method_version=receipt['method_version'],needs_visual_review=True,
                       confidence=receipt['confidence'] if state in OCR_TEXT_STATES else None,
                       ocr_receipt_id=receipt['receipt_id'],ocr_receipt_sha256=receipt['receipt_sha256'])
            if state in OCR_TEXT_STATES:row.update(status='partial',reason='ocr_visual_review_required')
            elif state=='blocked':row.update(status='blocked',reason=receipt['blocked_reason'])
            else:row.update(status='partial',reason='ocr_skipped_machine_text')
    return rows

def ocr_unit(page, receipt_id, receipt_sha256, text):
    """The unit carrying one page's OCR text; the text is the receipt's sidecar.txt."""
    return {'kind':'pdf_page','locator':{'page':page},'text':text,
            'data':{'method':OCR_METHOD,'ocr_receipt_id':receipt_id,'ocr_receipt_sha256':receipt_sha256,'visual_review':'not_done'}}

def merge_ocr_units(native_units, ocr_text_units):
    """Replace only the units of pages that received OCR text; every other unit is untouched.

    ``ocr_text_units`` maps page -> OCR unit. Pages without a native unit are appended in
    page order. Deterministic so enrollment can recompute it from bound artifacts.
    """
    merged=[];placed=set()
    for unit in native_units:
        page=unit.get('locator',{}).get('page') if isinstance(unit.get('locator'),dict) else None
        if type(page) is int and page in ocr_text_units:
            if page not in placed:merged.append(ocr_text_units[page]);placed.add(page)
            continue
        merged.append(unit)
    for page in sorted(set(ocr_text_units)-placed):merged.append(ocr_text_units[page])
    return merged

def ocr_summary(loaded):
    """Page-manifest view of a verified ``page_ocr.load_page_receipt`` result."""
    receipt=loaded['receipt']
    return {'receipt_id':receipt['receipt_id'],'receipt_sha256':loaded['receipt_sha256'],
            'status':receipt['status'],'blocked_reason':receipt['blocked_reason'],
            'confidence':receipt['confidence'],
            'method_version':receipt['tool_versions'].get('tesseract','unverified')}

def _child(source,sha,form,dest,root):
    if form in IMAGES or form=='msg':
        raw=_capture(source,sha)
        if form in IMAGES:
            units,parser,version=_image(source,raw);children=[]
        else:
            units,parser,version,children=_msg(source,raw,root)
        with (dest/'units.jsonl').open('x') as stream:
            for unit in units:stream.write(json.dumps(unit,ensure_ascii=True,sort_keys=True)+'\n')
        issues=[{'code':'msg_attachment_bytes_not_preserved'}] if any(u['kind']=='msg_attachment' and not u['data']['bytes_preserved'] for u in units) else []
        metadata={'stage':'partial' if issues else 'complete','counts':{'units':len(units)},
                  'issues':issues,'children':children,'parser':parser,'parser_version':version}
        if form=='msg':
            metadata.update(parser='legacy-intake:msg',
                parser_components={'intake':folder.VERSION+'/'+folder.ENGINE_REVISION,
                                   'extract-msg':version,'olefile':_version('olefile')},
                artifacts={'units.jsonl':hashlib.sha256((dest/'units.jsonl').read_bytes()).hexdigest()})
        _write(dest/'digest.json',metadata)
    else:
        folder.worker(Path(source),sha,form,dest,root,dict(folder.LIMITS),0)
        metadata=json.loads((dest/'digest.json').read_text())
        dependency={'pdf':'pypdf','xlsx':'openpyxl'}.get(form)
        metadata['parser_components']={'intake':folder.VERSION+'/'+folder.ENGINE_REVISION}
        if dependency:metadata['parser_components'][dependency]=_version(dependency)
        metadata['parser']='legacy-intake:'+form
        metadata['parser_version']=metadata['parser_components'].get(dependency,folder.VERSION+'/'+folder.ENGINE_REVISION)
        (dest/'digest.json').write_text(json.dumps(metadata,sort_keys=True,ensure_ascii=True)+'\n')

def run_bounded(args,timeout):
    """Private diagnostics are discarded; timeout terminates the whole child group."""
    proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait()
        raise
    return proc

def _read_output(dest):
    metadata_path=dest/'digest.json'
    if metadata_path.stat().st_size>MAX_UNIT_BYTES:raise ValueError('metadata_byte_bound')
    metadata=json.loads(metadata_path.read_text())
    units_path=dest/'units.jsonl'
    if units_path.stat().st_size>MAX_UNIT_BYTES:raise ValueError('unit_output_byte_bound')
    units=[]
    with units_path.open() as stream:
        for line in stream:
            if len(units)>=MAX_UNITS:raise ValueError('unit_count_bound')
            units.append(json.loads(line))
    parser=metadata.get('parser','unknown')
    version=metadata.get('parser_version','unknown')
    pages=page_manifest(units,metadata.get('counts',{}).get('pages_expected'),parser,version)
    return metadata,units,pages

def _ocr_pages(pinned_sha, intake_root, ocr_root, page_numbers, tools, tool_signature, attempt_id, *, timeout=180, runner=subprocess.run):
    """Per-page OCR (the only OCR path) for the given non-ok pages.

    Returns page -> {'summary': page-manifest receipt summary, 'text': sidecar text or None}.
    Text is used only for ocr_text_unreviewed/visual_check_queued receipts.
    """
    page_numbers=tuple(page_numbers)
    receipts=page_ocr.extract_image_only_pages(intake_root,pinned_sha,ocr_root,tool_signature=tool_signature,
        tools=tools,attempt_id=attempt_id,pages=page_numbers,timeout=timeout,runner=runner)
    if tuple(r['locator']['page'] for r in receipts)!=page_numbers:raise ValueError('ocr_receipt_page_mismatch')
    results={}
    for receipt in receipts:
        page=receipt['locator']['page']
        loaded=page_ocr.load_page_receipt(ocr_root,pinned_sha,page,receipt['receipt_id'])
        summary=ocr_summary(loaded)
        text=None
        if summary['status'] in OCR_TEXT_STATES:
            if loaded['sidecar'] is None:raise ValueError('ocr_receipt_text_missing')
            text=loaded['sidecar'].decode('utf-8')
        results[page]={'summary':summary,'text':text}
    return results

def apply_ocr(native_units, expected_pages, parser, parser_version, results):
    """Merge per-page OCR results onto native units/pages; native-text pages keep their units."""
    texts={page:ocr_unit(page,r['summary']['receipt_id'],r['summary']['receipt_sha256'],r['text']) for page,r in results.items() if r['text'] is not None}
    units=merge_ocr_units(native_units,texts)
    pages=page_manifest(units,expected_pages,parser,parser_version,ocr_receipts={page:r['summary'] for page,r in results.items()})
    receipts=[{'page':page,'receipt_id':r['summary']['receipt_id'],'receipt_sha256':r['summary']['receipt_sha256'],'status':r['summary']['status']} for page,r in sorted(results.items())]
    return units,pages,receipts

def _ocr_configuration(ocr_output_root, ocr_tools, tool_signature):
    """None when OCR is not configured at all; otherwise a validated configuration."""
    if ocr_output_root is None and ocr_tools is None and tool_signature is None:return None
    if ocr_output_root is None or ocr_tools is None or tool_signature is None:
        raise ValueError('ocr_configuration_incomplete: ocr_output_root, ocr_tools and tool_signature are all required')
    root=Path(ocr_output_root)
    if not root.is_absolute():raise ValueError('ocr_configuration_invalid: ocr_output_root must be absolute')
    try:_private_directory(root)
    except (ValueError,OSError):raise ValueError('ocr_configuration_invalid: ocr_output_root must be an existing private directory') from None
    try:page_ocr._resolved_tools(ocr_tools)
    except ValueError as error:raise ValueError('ocr_configuration_invalid: '+str(error)) from None
    if not isinstance(tool_signature,str) or not re.fullmatch(r"[\w .+/-]{1,120}",tool_signature):
        raise ValueError('ocr_configuration_invalid: tool_signature must be an operator-pinned version label')
    return {'root':root,'tools':ocr_tools,'tool_signature':tool_signature}

def extract(source,expected_sha,output_root,form=None,timeout=180,ocr_output_root=None,ocr_tools=None,tool_signature=None,ocr_attempt_id='initial',ocr_runner=subprocess.run):
    """Parse one immutable input in a child process; never promote review stages.

    OCR runs only through ``extract.ocr`` per-page receipts, only for PDF pages that are
    not ``ok``; native-text pages keep their units and page rows unchanged.
    """
    if type(timeout)is not int or not 1<=timeout<=600:raise ValueError('timeout bound')
    ocr_config=_ocr_configuration(ocr_output_root,ocr_tools,tool_signature)
    if not isinstance(ocr_attempt_id,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",ocr_attempt_id):
        raise ValueError('ocr_configuration_invalid: ocr_attempt_id must be a safe label')
    root=_private_directory(output_root);raw=_capture(source,expected_sha)
    form=form or folder.fmt(str(source),raw[:16384],raw)
    run=Path(tempfile.mkdtemp(prefix='extract-',dir=root));os.chmod(run,0o700)
    dest=run/'derived';dest.mkdir(mode=0o700);(run/'blobs').mkdir(mode=0o700)
    pinned=run/'input';fd=os.open(pinned,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
    with os.fdopen(fd,'wb') as stream:stream.write(raw)
    result={'schema':'format-extraction-v1','original_sha256':expected_sha,'route':route(form),'parser_adapter':VERSION,'status':'blocked','units':[],'pages':[],'issues':[],'review_status':'not_reviewed','original_changed':False,'run_path':str(run)}
    if form not in SUPPORTED|IMAGES|{'msg'}:
        result['issues']=[{'code':'decoder_needed','owner':'runtime'}]
    else:
        args=[sys.executable,'-B','-m',__name__,'_worker',str(pinned),expected_sha,form,str(dest),str(run)]
        try:
            child=run_bounded(args,timeout)
            if child.returncode:
                result['issues']=[{'code':'decoder_unavailable_or_failed','owner':'runtime','exit_code':child.returncode}]
            else:
                metadata,units,pages=_read_output(dest)
                result.update(status=metadata['stage'],units=units,pages=pages,issues=metadata.get('issues',[]),parser=metadata.get('parser','unknown'),parser_version=metadata.get('parser_version','unknown'),parser_components=metadata.get('parser_components',{}),children=metadata.get('children',[]))
                if form=='pdf' and any(p['status']!='ok' for p in pages):
                    result['status']='partial'
                    if ocr_config is None:
                        result['issues'].append({'code':'ocr_runtime_unconfigured','owner':'runtime'})
                    else:
                        _run_page_ocr(result,run,raw,expected_sha,metadata,ocr_config,ocr_attempt_id,timeout,ocr_runner)
        except subprocess.TimeoutExpired:
            result['status']='blocked';result['issues'].append({'code':'extraction_timeout','owner':'runtime'})
        except (ValueError,OSError,json.JSONDecodeError):
            result['status']='blocked';result['issues'].append({'code':'invalid_or_bounded_parser_output','owner':'runtime'})
    result['receipt_sha256']=_write(run/'extraction.json',result)
    return result

def _run_page_ocr(result,run,raw,expected_sha,metadata,config,attempt_id,timeout,runner):
    non_ok=[p['page_no'] for p in result['pages'] if p['status']!='ok']
    intake=run/'ocr-source';intake.mkdir(mode=0o700);(intake/'blobs').mkdir(mode=0o700)
    fd=os.open(intake/'blobs'/expected_sha,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
    with os.fdopen(fd,'wb') as stream:stream.write(raw)
    try:
        results=_ocr_pages(expected_sha,intake,config['root'],non_ok,config['tools'],config['tool_signature'],attempt_id,timeout=timeout,runner=runner)
    except Exception as error:  # bounded batch failure: native results stay partial
        result['issues'].append({'code':'ocr_failed','owner':'runtime','error_type':type(error).__name__})
        return
    expected=metadata.get('counts',{}).get('pages_expected')
    units,pages,receipts=apply_ocr(result['units'],expected,result['parser'],result['parser_version'],results)
    derived=run/'ocr-derived';derived.mkdir(mode=0o700)
    with (derived/'units.jsonl').open('x') as stream:
        for unit in units:stream.write(json.dumps(unit,ensure_ascii=True,sort_keys=True)+'\n')
    os.chmod(derived/'units.jsonl',0o600)
    _write(derived/'digest.json',{**metadata,'ocr_receipts':receipts})
    result['pre_ocr']={'status':result['status'],'unit_count':len(result['units']),'page_count':len(result['pages']),'issues':list(result['issues'])}
    result.update(units=units,pages=pages,status='partial',ocr_receipts=receipts,ocr_output_root=str(config['root']),ocr_method=OCR_METHOD)
    if any(r['status'] in OCR_TEXT_STATES for r in receipts):
        result['issues'].append({'code':'ocr_visual_review_required','owner':'visual-review'})
    blocked=[r['page'] for r in receipts if r['status']=='blocked']
    if blocked:result['issues'].append({'code':'ocr_pages_blocked','owner':'runtime','pages':blocked})

def main():
    if len(sys.argv)>1 and sys.argv[1]=='_worker':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(folder.LIMITS['memory_bytes'],folder.LIMITS['memory_bytes']))
        resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_UNIT_BYTES,MAX_UNIT_BYTES))
        os.umask(0o077)
        _child(sys.argv[2],sys.argv[3],sys.argv[4],Path(sys.argv[5]),Path(sys.argv[6]));return
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--sha256',required=True);p.add_argument('--output-root',required=True);p.add_argument('--format')
    p.add_argument('--ocr-output-root',help='private per-page OCR receipt root; enables OCR of non-ok PDF pages')
    p.add_argument('--ocr-tool-signature',help='operator-pinned OCR tool version label (required with --ocr-output-root)')
    p.add_argument('--ocr-attempt-id',default='initial')
    a=p.parse_args()
    tools=None
    if a.ocr_output_root or a.ocr_tool_signature:
        if not (a.ocr_output_root and a.ocr_tool_signature):p.error('--ocr-output-root and --ocr-tool-signature are required together')
        readiness=page_ocr.dependency_status()
        if not readiness['ready']:
            print(json.dumps({'status':'blocked','issues':[{'code':'ocr_runtime_unconfigured','owner':'runtime','missing':readiness['missing'],'version_unverified':readiness['version_unverified']}]}))
            raise SystemExit(2)
        tools=readiness['tools']
    r=extract(a.source,a.sha256,a.output_root,a.format,ocr_output_root=a.ocr_output_root,ocr_tools=tools,tool_signature=a.ocr_tool_signature,ocr_attempt_id=a.ocr_attempt_id)
    print(json.dumps(stdout_summary(r)))

PRIVATE_PATH_FIELDS = {'run_path','ocr_output_root'}
def stdout_summary(result):
    """Operator summary without units or absolute private paths; run_id is the run directory name under --output-root."""
    summary={k:v for k,v in result.items() if k not in {'units','children'}|PRIVATE_PATH_FIELDS}
    if result.get('run_path'):summary['run_id']=Path(result['run_path']).name
    return summary
if __name__=='__main__':main()
