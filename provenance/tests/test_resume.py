"""Continue completed capture, never repeat instrumentation or trust old proofs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow import native
from yosys_flow.capture import resume
from yosys_flow.cli import parser
from yosys_flow.common import TraceError, digest, write_json
from yosys_flow.runner import run, load_run, query


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built')
class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='resume-capture-')
        cls.root = Path(cls.temp.name)
        cls.source = cls.root/'source with spaces.v'
        cls.header = cls.root/'ops.vh'
        cls.header.write_text('`define COMB(a,b) ((a)&(b))\n')
        cls.source.write_text('''`include "ops.vh"
module leaf #(parameter W=2)(input [W-1:0] a,b, output [W-1:0] y);
assign y = `COMB(a,b);
endmodule
module demo #(parameter W=2)(input [W-1:0] a,b,c, output [W-1:0] y,z);
leaf #(W) first(a,b,y);
leaf #(W) second(b,c,z);
endmodule
''')
        cls.captures = {}
        for mode, extra in [('retained',[]), ('flatten',['--flatten']), ('m1',['--flow','m1'])]:
            out = cls.root/mode
            with patch.object(native, 'NativeTrace', side_effect=TraceError('stop after capture')):
                try:
                    run(parser().parse_args(['run','--source',str(cls.source),'--top','demo',
                        '--out',str(out),'--native-yosys',str(NATIVE),'-I',str(cls.root),
                        '-D','TEST=1','--param','W=2',*extra]))
                except TraceError as exc:
                    if str(exc) != 'stop after capture': raise
                else: raise AssertionError('capture-only fixture did not stop')
            cls.captures[mode] = out

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def options(self, source, out, *extra):
        return parser().parse_args(['resume',str(source),'--out',str(out),*extra])

    def copy(self, name, legacy=False):
        out = self.root/(self._testMethodName+'-'+name)
        shutil.copytree(self.captures['retained'],out)
        if legacy:
            m = json.loads((out/'manifest.json').read_text())
            for key in ('capture','capture_started_ns','source_inputs_before_capture'):
                m.pop(key,None)
            m.update(inputs=[],artifacts={},timings_seconds={},status='running')
            m['tool'].pop('abc',None)
            (out/'capture.json').unlink()
            write_json(out/'manifest.json',m)
        return out

    def assert_failed_archive(self, out):
        self.assertFalse((out/'verified-index.sqlite3').exists())
        self.assertFalse((out/'verified-index.json.gz').exists())
        if (out/'manifest.json').exists():
            self.assertNotEqual(json.loads((out/'manifest.json').read_text())['status'],'complete')

    def test_resume_retained_flatten_and_checkpoint_without_native_synthesis(self):
        original_run = subprocess.run
        calls = []
        def guarded(command, *args, **kwargs):
            if '-s' in command:
                calls.append((str(Path(command[0]).resolve()),Path(command[-1]).name))
                self.assertNotEqual(Path(command[0]).resolve(),NATIVE.resolve())
                self.assertNotEqual(Path(command[-1]).name,'traced.ys')
            return original_run(command,*args,**kwargs)
        for mode, capture in self.captures.items():
            out = self.root/('resumed-'+mode)
            before = {p.name:(digest(p),p.stat().st_mtime_ns) for p in capture.iterdir() if p.is_file()}
            extra = ['--index-backend','gzip'] if mode=='flatten' else []
            with patch('subprocess.run', side_effect=guarded): result=resume(self.options(capture,out,*extra))
            self.assertTrue(all(result['checks'].values()))
            timings = {}
            manifest,trace,_ = load_run(out,timings)
            self.assertEqual(timings['verified_index_status'],'hit')
            self.assertEqual(manifest['run_id'],json.loads((capture/'manifest.json').read_text())['run_id'])
            self.assertNotIn('traced',manifest['timings_seconds'])
            self.assertIn('baseline',manifest['timings_seconds'])
            self.assertTrue(manifest['resume']['reused_capture'])
            self.assertEqual(before,{p.name:(digest(p),p.stat().st_mtime_ns) for p in capture.iterdir() if p.is_file()})
            self.assertNotEqual((capture/'yosys.jsonl').stat().st_ino,(out/'yosys.jsonl').stat().st_ino)
            self.assertEqual((capture/'yosys.jsonl').read_bytes(),(out/'yosys.jsonl').read_bytes())
            selected=query(parser().parse_args(['query',str(out),'--source',str(self.source),'--line','3']))
            self.assertTrue(selected['final_cells'])
        self.assertTrue(any(name=='baseline.ys' for _,name in calls))
        self.assertTrue(any(name=='equivalence.ys' for _,name in calls))

    def test_legacy_capture_uses_guard_and_ignores_unverified_prior_results(self):
        capture = self.copy('legacy',legacy=True)
        (capture/'validation.json').write_text('{"valid":true}')
        (capture/'verified-index.sqlite3').write_text('untrusted partial index')
        out=self.root/'legacy-complete'
        result=resume(self.options(capture,out))
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(json.loads((out/'capture.json').read_text())['input_binding_basis'],'legacy_timestamp_guard')
        self.assertNotEqual((out/'verified-index.sqlite3').read_bytes(),(capture/'verified-index.sqlite3').read_bytes())

    def test_skip_only_stock_equivalence_publishes_loadable_index_with_explicit_status(self):
        original_run=subprocess.run
        calls=[]
        def guarded(command,*args,**kwargs):
            if '-s' in command:
                name=Path(command[-1]).name
                calls.append(name)
                self.assertNotIn(name,('equivalence.ys','traced.ys'))
            return original_run(command,*args,**kwargs)
        for mode,backend in [('retained','sqlite'),('flatten','gzip')]:
            out=self.root/('skipped-'+mode)
            with patch('subprocess.run',side_effect=guarded):
                result=resume(self.options(self.captures[mode],out,'--skip-stock-equivalence',
                                           '--index-backend',backend))
            self.assertTrue(result['valid'])
            self.assertIsNone(result['checks']['stock_to_traced_equivalence'])
            self.assertIn('stock_to_traced_equivalence',result['skipped_checks'])
            self.assertTrue(result['checks']['stock_flow_exact_match'])
            self.assertTrue(result['checks']['abc_internal_evidence_validated'])
            self.assertFalse((out/'equivalence.log').exists())
            for use_index in (True,False):
                timings={}
                m,_,validation=load_run(out,timings,use_index=use_index)
                self.assertEqual(timings['verified_index_status'],'hit' if use_index else 'disabled')
                self.assertEqual(m['validation_policy'],validation['validation_policy'])
                self.assertIsNone(validation['checks']['stock_to_traced_equivalence'])
            self.assertNotIn('equivalence',m['timings_seconds'])
        self.assertIn('baseline.ys',calls)
        self.assertIn('instance_equivalence.ys',calls)
        # Skipping is an explicit option for each invocation, never inherited.
        retry=self.root/'skipped-then-fully-verified'
        full=resume(self.options(self.root/'skipped-flatten',retry))
        self.assertTrue(full['checks']['stock_to_traced_equivalence'])
        self.assertTrue((retry/'equivalence.log').is_file())

    def test_skip_does_not_accept_forged_proof_success(self):
        out=self.root/'skipped-forged'
        resume(self.options(self.captures['flatten'],out,'--skip-stock-equivalence'))
        m=json.loads((out/'manifest.json').read_text())
        validation=json.loads((out/'validation.json').read_text())
        validation['checks']['stock_to_traced_equivalence']=True
        write_json(out/'validation.json',validation)
        with self.assertRaisesRegex(TraceError,'explicitly unproved'):
            native._check_saved_reference(out,m)
        validation['checks']['stock_to_traced_equivalence']=None
        write_json(out/'validation.json',validation)
        m.pop('validation_policy')
        with self.assertRaisesRegex(TraceError,'not validated'):
            native._check_saved_reference(out,m)

    def test_skip_still_rejects_abc_evidence_failure(self):
        out=self.root/'skip-abc-failed'
        with patch.object(native.NativeTrace,'compare_abc_artifacts',side_effect=TraceError('ABC evidence rejected')):
            with self.assertRaisesRegex(TraceError,'ABC evidence rejected'):
                resume(self.options(self.captures['flatten'],out,'--skip-stock-equivalence'))
        self.assert_failed_archive(out)

    def test_skip_still_rejects_stock_structure_mismatch(self):
        out=self.root/'skip-stock-mismatch'
        original_run=subprocess.run
        def altered(command,*args,**kwargs):
            result=original_run(command,*args,**kwargs)
            if '-s' in command and Path(command[-1]).name=='baseline.ys':
                file=Path(command[-1]).with_name('baseline.json')
                data=json.loads(file.read_text());data['modules']['demo']['cells']={}
                write_json(file,data)
            return result
        with patch('subprocess.run',side_effect=altered):
            with self.assertRaisesRegex(TraceError,'native result differs from stock flow'):
                resume(self.options(self.captures['flatten'],out,'--skip-stock-equivalence'))
        self.assert_failed_archive(out)

    def test_changed_source_or_include_is_rejected_even_if_mtime_is_restored(self):
        for source in (self.source,self.header):
            content, stat = source.read_bytes(),source.stat()
            out = self.root/('changed-'+source.suffix[1:])
            try:
                source.write_bytes(content+b'// changed after capture\n')
                os.utime(source,ns=(stat.st_atime_ns,stat.st_mtime_ns))
                with self.assertRaisesRegex(TraceError,'input hash changed'):
                    resume(self.options(self.captures['retained'],out))
                self.assert_failed_archive(out)
            finally:
                source.write_bytes(content);os.utime(source,ns=(stat.st_atime_ns,stat.st_mtime_ns))

    def test_legacy_source_touched_after_capture_is_rejected(self):
        capture=self.copy('legacy',legacy=True)
        stat=self.header.stat();out=self.root/'legacy-touched'
        try:
            os.utime(self.header,None)
            with self.assertRaisesRegex(TraceError,'input changed after capture'):
                resume(self.options(capture,out))
            self.assert_failed_archive(out)
        finally: os.utime(self.header,ns=(stat.st_atime_ns,stat.st_mtime_ns))

    def test_changed_capture_file_checkpoint_and_configuration_are_rejected(self):
        for mode in ('jsonl','checkpoint','configuration','native'):
            capture=self.copy(mode);out=self.root/('reject-'+mode)
            if mode=='jsonl':
                p=capture/'yosys.jsonl';p.write_text(p.read_text().replace('run_end','bad_end'))
            elif mode=='checkpoint':
                with (capture/'capture.json').open('a') as stream:stream.write(' ')
            else:
                m=json.loads((capture/'manifest.json').read_text())
                if mode=='configuration':m['configuration']['defines']=['TEST=2']
                else:m['tool']['native_sha256']='0'*64
                write_json(capture/'manifest.json',m)
            with self.subTest(mode=mode),self.assertRaises(TraceError):resume(self.options(capture,out))
            self.assert_failed_archive(out)

    def test_internal_proof_failure_never_publishes_and_can_retry(self):
        out=self.root/'proof-failed'
        with patch.object(native.NativeTrace,'compare_abc_artifacts',side_effect=TraceError('ABC evidence rejected')):
            with self.assertRaisesRegex(TraceError,'ABC evidence rejected'):
                resume(self.options(self.captures['retained'],out))
        self.assert_failed_archive(out)
        self.assertFalse((out/'baseline.log').exists())
        retry=self.root/'proof-retried'
        self.assertTrue(all(resume(self.options(out,retry))['checks'].values()))

    def test_copy_mutation_is_rejected_without_affecting_original(self):
        capture=self.captures['retained'];out=self.root/'copy-failed'
        original=shutil.copy2
        before=digest(capture/'after.json')
        def changed(source,target,*args,**kwargs):
            result=original(source,target,*args,**kwargs)
            if Path(target).name=='after.json':Path(target).write_text('{}')
            return result
        with patch('yosys_flow.capture.shutil.copy2',side_effect=changed):
            with self.assertRaisesRegex(TraceError,'capture changed during copy'):
                resume(self.options(capture,out))
        self.assert_failed_archive(out)
        self.assertEqual(digest(capture/'after.json'),before)

    def test_uncheckpointed_resume_cannot_fall_back_to_legacy_binding(self):
        capture=self.captures['retained'];out=self.root/'copy-not-checkpointed'
        with patch('yosys_flow.capture.publish_checkpoint',side_effect=TraceError('copy interrupted')):
            with self.assertRaisesRegex(TraceError,'copy interrupted'):
                resume(self.options(capture,out))
        self.assert_failed_archive(out)
        # Even a complete raw copy is not a published, bound capture yet.
        self.assertTrue((out/'yosys.jsonl').is_file())
        self.assertFalse((out/'capture.json').exists())
        retry=self.root/'copy-not-checkpointed-retry'
        with self.assertRaisesRegex(TraceError,'resume from the original directory'):
            resume(self.options(out,retry))
        self.assertFalse(retry.exists())

    def test_existing_or_nested_destination_is_rejected(self):
        capture=self.captures['retained']
        for out in (capture,capture/'nested',self.captures['flatten']):
            with self.subTest(out=out),self.assertRaises(TraceError):resume(self.options(capture,out))
        self.assertFalse((capture/'nested').exists())
