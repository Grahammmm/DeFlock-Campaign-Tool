"""Synthetic format, resource and page-coverage acceptance cases."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib
from campaign_tool.records import extraction_routes as er

class ExtractionRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.root.chmod(0o700)
    def tearDown(self):self.tmp.cleanup()
    def run_file(self,data,form):
        source=self.root/('input.'+form);source.write_bytes(data)
        out=self.root/'output';out.mkdir(mode=0o700,exist_ok=True)
        result=er.extract(source,hashlib.sha256(data).hexdigest(),out,form,timeout=20)
        self.assertEqual(source.read_bytes(),data)
        self.assertEqual(result['review_status'],'not_reviewed')
        return result
    def test_inventory_routes_every_unfinished_format(self):
        rows=[{'sha256':'fixture','format':f} for f in ('png','msg','pdf','weird')]
        self.assertEqual(len(er.inventory(rows)),4)
        self.assertTrue(er.route('weird')['route'].startswith('decoder-needed:'))
    def test_low_value_only_proposed(self):
        self.assertEqual(er.route('png','inline_branding')['route'],'low-value-proposed')
        self.assertTrue(er.route('png','inline_branding')['review_required'])
    def test_plain_text_real_worker(self):
        r=self.run_file(b'first line\nsecond line\n','txt')
        self.assertEqual(r['status'],'complete');self.assertEqual(len(r['units']),2)
        self.assertEqual(r['units'][1]['locator'],{'line':2})
    def test_csv_empty_redacted_and_na_preserved(self):
        r=self.run_file(b'reason,value\n,1\n[redacted],2\nn/a,3\n','csv')
        self.assertEqual(r['status'],'complete')
        states=[u['data']['presence'][0] for u in r['units'] if u['kind']=='csv_row']
        self.assertEqual(states,['empty','redacted_marker','present'])
    def test_docx_reuses_container_parser(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            z.writestr('[Content_Types].xml','<Types/>')
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic paragraph</w:t></w:r></w:p></w:body></w:document>')
        r=self.run_file(buf.getvalue(),'docx');self.assertEqual(r['units'][0]['text'],'Synthetic paragraph')
    def test_unsafe_archive_member_never_preserved(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:z.writestr('../escape.txt','synthetic')
        r=self.run_file(buf.getvalue(),'zip');self.assertEqual(r['status'],'partial')
        self.assertEqual(r['children'],[])
    def test_missing_decoder_is_explicit(self):
        r=self.run_file(b'synthetic unsupported','unknown');self.assertEqual(r['status'],'blocked')
        self.assertEqual(r['issues'][0]['code'],'decoder_needed')
    def test_bad_hash_refused(self):
        p=self.root/'a.txt';p.write_bytes(b'fixture')
        with self.assertRaisesRegex(ValueError,'hash'):er.extract(p,'0'*64,self.root,'txt')
    def test_symlink_source_refused(self):
        p=self.root/'a.txt';p.write_bytes(b'fixture');s=self.root/'alias';s.symlink_to(p)
        with self.assertRaises(OSError):er.extract(s,hashlib.sha256(b'fixture').hexdigest(),self.root,'txt')
    def test_output_requires_private_directory(self):
        p=self.root/'a.txt';p.write_bytes(b'fixture');self.root.chmod(0o755)
        with self.assertRaises(ValueError):er.extract(p,hashlib.sha256(b'fixture').hexdigest(),self.root,'txt')
    def test_mixed_pages_and_missing_page_explicit(self):
        units=[{'locator':{'page':1},'text':'text','data':{}},{'locator':{'page':2},'text':'','data':{'ocr_needed':True}}]
        pages=er.page_manifest(units,3,'fixture-parser','1')
        self.assertEqual([p['status'] for p in pages],['ok','partial','blocked'])
        self.assertTrue(all(p['confidence'] is None and p['needs_visual_review'] for p in pages))
    def test_duplicate_page_not_double_coverage(self):
        with self.assertRaises(ValueError):er.page_manifest([{'locator':{'page':1}},{'locator':{'page':1}}],1)
    def test_ocr_text_still_requires_visual_check(self):
        r=er.page_manifest([{'locator':{'page':1},'text':'uncertain','data':{}}],1,'ocr','1',ocr=True)
        self.assertEqual(r[0]['status'],'partial');self.assertIsNone(r[0]['confidence'])
    def test_png_dimensions(self):
        try:import PIL
        except ImportError:self.skipTest('Pillow optional parser unavailable')
        def chunk(t,d):return struct.pack('!I',len(d))+t+d+struct.pack('!I',zlib.crc32(t+d)&0xffffffff)
        raw=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00\xff\xff\xff'))+chunk(b'IEND',b'')
        r=self.run_file(raw,'png');self.assertEqual(r['status'],'complete');self.assertEqual(r['units'][0]['data']['width'],1)
    def test_blank_pdf_has_page_state(self):
        try:from pypdf import PdfWriter
        except ImportError:self.skipTest('pypdf optional parser unavailable')
        w=PdfWriter();w.add_blank_page(width=72,height=72);buf=io.BytesIO();w.write(buf)
        r=self.run_file(buf.getvalue(),'pdf');self.assertEqual(r['status'],'partial')
        self.assertEqual(len(r['pages']),1);self.assertEqual(r['pages'][0]['reason'],'ocr_needed_or_blank')
        self.assertIn('ocr_runtime_unconfigured',[i['code'] for i in r['issues']])
    def test_worker_timeout_is_accounted(self):
        import subprocess
        with patch.object(er,'run_bounded',side_effect=subprocess.TimeoutExpired('fixture',1)):
            r=self.run_file(b'text','txt')
        self.assertEqual(r['status'],'blocked');self.assertEqual(r['issues'][0]['code'],'extraction_timeout')
    def test_invalid_timeout_rejected(self):
        with self.assertRaises(ValueError):er.extract('unused','unused',self.root,timeout=True)

    def test_msg_decoded_attachment_preserved_with_locator(self):
        import sys,types
        data=b'synthetic attachment'
        attachment=types.SimpleNamespace(longFilename='fixture.txt',data=data)
        class Message:
            body='synthetic body';attachments=[attachment]
            def __enter__(self):return self
            def __exit__(self,*args):return False
        module=types.SimpleNamespace(openMsg=lambda _:Message())
        (self.root/'blobs').mkdir()
        with patch.dict(sys.modules,{'extract_msg':module}):
            units,parser,version,children=er._msg('synthetic',b'synthetic',self.root)
        self.assertEqual(children[0]['locator'],{'attachment':1})
        self.assertEqual((self.root/'blobs'/children[0]['sha']).read_bytes(),data)
        self.assertEqual(units[-1]['data']['bytes_preserved'],True)
    def test_embedded_msg_not_falsely_called_original_bytes(self):
        import sys,types
        class Message:
            body='synthetic';attachments=[types.SimpleNamespace(data=object())]
            def __enter__(self):return self
            def __exit__(self,*args):return False
        with patch.dict(sys.modules,{'extract_msg':types.SimpleNamespace(openMsg=lambda _:Message())}):
            units,_,_,children=er._msg('synthetic',b'synthetic',self.root)
        self.assertEqual(children,[]);self.assertTrue(units[-1]['data']['needs_attachment_decoder'])

    def test_timeout_kills_entire_worker_group(self):
        import subprocess,signal
        from unittest.mock import Mock
        proc=Mock(pid=12345);proc.wait.side_effect=[subprocess.TimeoutExpired('fixture',1),-9]
        with patch.object(er.subprocess,'Popen',return_value=proc) as launch,patch.object(er.os,'killpg') as kill:
            with self.assertRaises(subprocess.TimeoutExpired):er.run_bounded(['fixture'],1)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual(launch.call_args.kwargs['stdout'],subprocess.DEVNULL)
        self.assertEqual(launch.call_args.kwargs['stderr'],subprocess.DEVNULL)
        kill.assert_called_once_with(12345,signal.SIGKILL)
        self.assertEqual(proc.wait.call_count,2)


class OCRReextractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def output(self,count=1):
        dest=self.root/'ocr-derived';dest.mkdir()
        (dest/'digest.json').write_text(json.dumps({'stage':'complete','counts':{'pages_expected':count},'parser':'fixture-pdf','parser_version':'1'}))
        (dest/'units.jsonl').write_text(json.dumps({'kind':'pdf_page','locator':{'page':1},'text':'synthetic OCR text','data':{}})+'\n')
        return dest
    def test_reextraction_preserves_page_provenance_and_visual_hold(self):
        dest=self.output()
        from unittest.mock import Mock
        with patch.object(Path,'mkdir'),patch.object(er,'run_bounded',return_value=Mock(returncode=0)):
            meta,units,pages=er._reextract_ocr(self.root/'ocr.pdf','d'*64,self.root,[{'page_no':1}],10)
        self.assertEqual(units[0]['locator'],{'page':1})
        self.assertEqual(pages[0]['source_derivative_sha256'],'d'*64)
        self.assertEqual(pages[0]['status'],'partial')
        self.assertTrue(pages[0]['needs_visual_review'])
    def test_changed_ocr_page_count_rejected(self):
        self.output(2)
        from unittest.mock import Mock
        with patch.object(Path,'mkdir'),patch.object(er,'run_bounded',return_value=Mock(returncode=0)):
            with self.assertRaisesRegex(ValueError,'page_count_changed'):
                er._reextract_ocr(self.root/'ocr.pdf','d'*64,self.root,[{'page_no':1}],10)
    def test_decoder_failure_remains_blocked(self):
        from unittest.mock import Mock
        with patch.object(er,'run_bounded',return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(ValueError,'reextraction_failed'):
                er._reextract_ocr(self.root/'ocr.pdf','d'*64,self.root,[{'page_no':1}],10)
    def test_parser_metadata_bound(self):
        dest=self.output()
        with patch.object(er,'MAX_UNIT_BYTES',1):
            with self.assertRaisesRegex(ValueError,'metadata_byte_bound'):er._read_output(dest)
    def test_each_image_frame_has_a_locator(self):
        import sys,types
        class Image:
            MAX_IMAGE_PIXELS=None
            n_frames=3;format='TIFF';size=(2,3)
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def seek(self,frame):pass
            def load(self):pass
        image=Image()
        module=types.SimpleNamespace(Image=types.SimpleNamespace(open=lambda _:image))
        with patch.dict(sys.modules,{'PIL':module}):units,_,_=er._image(None,b'fixture')
        self.assertEqual([x['locator']['frame'] for x in units],[1,2,3])
        self.assertTrue(all(x['data']['visual_review']=='not_done' for x in units))

class MainOutputTests(unittest.TestCase):
    def test_main_stdout_omits_absolute_private_paths(self):
        import contextlib,sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o700);data=b'synthetic line\n'
            source=root/'input.txt';source.write_bytes(data);out=root/'output';out.mkdir(mode=0o700)
            real=er.extract
            def extract(*args,**kwargs):
                result=real(*args,**kwargs)
                result['ocr_derivative_path']=result['run_path']+'/ocr.pdf'
                return result
            argv=['extraction_routes','--source',str(source),'--sha256',hashlib.sha256(data).hexdigest(),'--output-root',str(out),'--format','txt']
            stream=io.StringIO()
            with patch.object(sys,'argv',argv),patch.object(er,'extract',side_effect=extract),contextlib.redirect_stdout(stream):er.main()
            printed=stream.getvalue();summary=json.loads(printed)
            self.assertNotIn(tmp,printed)
            self.assertFalse({'run_path','ocr_derivative_path','units','children'}&set(summary))
            self.assertEqual(summary['status'],'complete')
            self.assertEqual([p.name for p in out.iterdir()],[summary['run_id']])
