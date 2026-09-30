"""Bounded extraction routes and page accounting around the existing intake worker."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import signal
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from .intake import folder

VERSION = 'format-routes-1'
SUPPORTED = {'pdf','eml','csv','tsv','docx','xlsx','zip','txt','md','json','jsonl','xml','html','htm','log','rst','yaml','yml'}
IMAGES = {'png','jpg','jpeg','gif','tif','tiff','webp','bmp'}
MAX_UNITS = 100000
MAX_UNIT_BYTES = 64*1024*1024

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
    import extract_msg
    units=[];children=[];expanded=0
    with extract_msg.openMsg(str(source)) as message:
        if not hasattr(message,'body'):raise ValueError('unsupported_msg_type')
        headers={k:str(getattr(message,k,None) or '') for k in ('subject','sender','to','cc','date')}
        units.append({'kind':'msg_headers','locator':{'property':'headers'},'text':'','data':headers})
        units.append({'kind':'msg_body','locator':{'property':'body'},'text':message.body or '', 'data':{'representation':'decoded_body'}})
        attachments=list(getattr(message,'attachments',[]))
        if len(attachments)>10000:raise ValueError('msg_attachment_bound')
        for i,attachment in enumerate(attachments,1):
            name=str(getattr(attachment,'longFilename',None) or getattr(attachment,'shortFilename',None) or '')
            data=getattr(attachment,'data',None)
            preserved=isinstance(data,bytes) and len(data)<=folder.LIMITS['member_bytes'] and expanded+len(data)<=folder.LIMITS['expanded_bytes']
            item={'filename':name,'bytes_preserved':preserved,'needs_attachment_decoder':not preserved}
            if preserved:
                child_sha=hashlib.sha256(data).hexdigest();target=root/'blobs'/child_sha
                if not target.exists():
                    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
                    with os.fdopen(fd,'wb') as stream:stream.write(data)
                elif hashlib.sha256(target.read_bytes()).hexdigest()!=child_sha:raise ValueError('child_hash_conflict')
                expanded+=len(data);item['sha256']=child_sha
                children.append({'sha':child_sha,'bytes':len(data),'name':name,'locator':{'attachment':i},'relation':'decoded_msg_attachment','format':folder.fmt(name,data[:16384],data)})
            units.append({'kind':'msg_attachment','locator':{'attachment':i},'text':'','data':item})
    return units,'extract-msg',_version('extract-msg'),children

def page_manifest(units, expected_pages=None, parser='unknown', parser_version='unknown', ocr=False):
    rows=[];seen=set()
    for unit in units:
        loc=unit.get('locator',{});page=loc.get('page')
        if page is None:continue
        if type(page)is not int or page<1 or page in seen:raise ValueError('invalid_or_duplicate_page_locator')
        seen.add(page);data=unit.get('data',{});text=unit.get('text','')
        blocked=bool(data.get('error'));needs=ocr or bool(data.get('ocr_needed')) or not str(text).strip()
        rows.append({'page_no':page,'method':parser,'method_version':parser_version,
                     'derivative_sha256':hashlib.sha256(str(text).encode()).hexdigest(),
                     'confidence':None,'needs_visual_review':True,
                     'status':'blocked' if blocked else 'partial' if needs else 'ok',
                     'reason':'page_error' if blocked else 'ocr_visual_review_required' if ocr else 'ocr_needed_or_blank' if needs else None})
    if expected_pages is not None:
        if type(expected_pages)is not int or expected_pages<0 or expected_pages>100000:raise ValueError('page_bound')
        if any(p>expected_pages for p in seen):raise ValueError('page_outside_denominator')
        for page in range(1,expected_pages+1):
            if page not in seen:rows.append({'page_no':page,'method':parser,'method_version':parser_version,'derivative_sha256':None,'confidence':None,'needs_visual_review':True,'status':'blocked','reason':'page_not_extracted'})
    return sorted(rows,key=lambda x:x['page_no'])

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
        _write(dest/'digest.json',{'stage':'partial' if issues else 'complete','counts':{'units':len(units)},'issues':issues,'children':children, 'parser':parser,'parser_version':version})
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

def _reextract_ocr(derivative,derivative_sha,run,original_pages,timeout):
    dest=run/'ocr-derived';dest.mkdir(mode=0o700)
    child=run_bounded([sys.executable,'-B','-m',__name__,'_worker',str(derivative),derivative_sha,'pdf',str(dest),str(run)],timeout)
    if child.returncode:raise ValueError('ocr_reextraction_failed')
    metadata,units,pages=_read_output(dest)
    expected=metadata.get('counts',{}).get('pages_expected')
    if type(expected)is not int or expected!=len(original_pages):raise ValueError('ocr_page_count_changed')
    pages=page_manifest(units,expected,metadata.get('parser','unknown'),metadata.get('parser_version','unknown'),ocr=True)
    for page in pages:page['source_derivative_sha256']=derivative_sha
    return metadata,units,pages

def extract(source,expected_sha,output_root,form=None,timeout=180,ocr_executable=None):
    """Parse one immutable input in a child process; never promote review stages."""
    if type(timeout)is not int or not 1<=timeout<=600:raise ValueError('timeout bound')
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
                    if ocr_executable:
                        executable=Path(ocr_executable)
                        if not executable.is_absolute() or not executable.is_file():raise ValueError('configured_ocr_executable_required')
                        derivative=run/'ocr.pdf'
                        completed=run_bounded([sys.executable,'-B','-m',__name__,'_ocr',str(executable),'--jobs','1','--skip-text','--rotate-pages','--deskew','--output-type','pdf',str(pinned),str(derivative)],timeout)
                        if completed.returncode==0 and derivative.is_file():
                            with derivative.open('rb') as stream:ocr_raw=stream.read(folder.LIMITS['source_bytes']+1)
                            if len(ocr_raw)>folder.LIMITS['source_bytes'] or not ocr_raw.startswith(b'%PDF-'):raise ValueError('invalid_ocr_output')
                            result['ocr_derivative_sha256']=hashlib.sha256(ocr_raw).hexdigest()
                            result['ocr_derivative_path']=str(derivative)
                            ocr_meta,ocr_units,ocr_pages=_reextract_ocr(derivative,result['ocr_derivative_sha256'],run,pages,timeout)
                            result['pre_ocr']={'status':result['status'],'unit_count':len(units),'page_count':len(pages),'issues':list(result['issues'])}
                            result.update(units=ocr_units,pages=ocr_pages,parser=ocr_meta.get('parser','unknown'),parser_version=ocr_meta.get('parser_version','unknown'),parser_components=ocr_meta.get('parser_components',{}),issues=ocr_meta.get('issues',[]),status='partial')
                            result['ocr_tool_version']='unverified'
                            result['issues'].append({'code':'ocr_visual_review_required','owner':'visual-review'})
                        else:result['issues'].append({'code':'ocr_failed','owner':'runtime','exit_code':completed.returncode})
                    else:result['issues'].append({'code':'ocr_runtime_unconfigured','owner':'runtime'})
        except subprocess.TimeoutExpired:
            result['status']='blocked';result['issues'].append({'code':'extraction_timeout','owner':'runtime'})
        except (ValueError,OSError,json.JSONDecodeError):
            result['status']='blocked';result['issues'].append({'code':'invalid_or_bounded_parser_output','owner':'runtime'})
    result['receipt_sha256']=_write(run/'extraction.json',result)
    return result

def main():
    if len(sys.argv)>1 and sys.argv[1] in {'_worker','_ocr'}:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(folder.LIMITS['memory_bytes'],folder.LIMITS['memory_bytes']))
        file_cap=folder.LIMITS['source_bytes'] if sys.argv[1]=='_ocr' else MAX_UNIT_BYTES
        resource.setrlimit(resource.RLIMIT_FSIZE,(file_cap,file_cap))
        os.umask(0o077)
        if sys.argv[1]=='_ocr':
            os.execv(sys.argv[2],sys.argv[2:])
        _child(sys.argv[2],sys.argv[3],sys.argv[4],Path(sys.argv[5]),Path(sys.argv[6]));return
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--sha256',required=True);p.add_argument('--output-root',required=True);p.add_argument('--format');p.add_argument('--ocr-executable')
    a=p.parse_args();r=extract(a.source,a.sha256,a.output_root,a.format,ocr_executable=a.ocr_executable)
    print(json.dumps({k:v for k,v in r.items() if k not in {'units','children'}}))
if __name__=='__main__':main()
