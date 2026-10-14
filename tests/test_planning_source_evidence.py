import hashlib,unittest
from do_again.supervisor.planning_source_evidence import summarize_public_sources

class PublicSourceEvidenceTests(unittest.TestCase):
    def test_growing_regressions_become_explicit_inventory_without_losing_implementation(self):
        implementation=b'def classify(x): return x\n';tests=b'# large public fixtures\n'+b'# x\n'*10000+b'def test_existing_guard(): pass\n'
        full,summary=summarize_public_sources({'tests.py':tests,'source.py':implementation},budget=4096,implementation_paths=['source.py'])
        self.assertEqual(full,{'source.py':implementation.decode()});self.assertEqual(summary['tests.py']['sha256'],hashlib.sha256(tests).hexdigest())
        self.assertEqual(summary['tests.py']['bytes'],len(tests));self.assertFalse(summary['tests.py']['full_content_exposed'])
        self.assertEqual(summary['tests.py']['top_level_definitions'],['test_existing_guard'])
    def test_inventory_names_and_parser_input_remain_bounded(self):
        data=''.join('def test_long_'+str(n)+'(): pass\n' for n in range(200)).encode()
        full,summary=summarize_public_sources({'tests.py':data},budget=0,implementation_paths=[])
        self.assertEqual(full,{});self.assertEqual(len(summary['tests.py']['top_level_definitions']),48);self.assertTrue(summary['tests.py']['inventory_truncated'])
        huge=b'#'+b'x'*(1024*1024)
        full,summary=summarize_public_sources({'huge.py':huge},budget=0,implementation_paths=[])
        self.assertEqual(summary['huge.py']['inventory_reason'],'file_exceeds_bounded_ast_input')
    def test_non_utf8_and_invalid_syntax_are_not_falsely_exposed_as_full_source(self):
        full,summary=summarize_public_sources({'binary.py':b'\xff','bad.py':b'def not valid'},budget=0,implementation_paths=[])
        self.assertEqual(full,{})
        self.assertEqual(summary['binary.py']['inventory_reason'],'not_utf8')
        self.assertEqual(summary['bad.py']['inventory_reason'],'source_not_parseable')
    def test_small_original_files_are_exposed_byte_for_byte(self):
        files={'source.py':b'print(1)\n','tests.py':b'assert True\n'}
        full,summary=summarize_public_sources(files,budget=4096,implementation_paths=['source.py'])
        self.assertEqual(full,{k:v.decode() for k,v in files.items()});self.assertEqual(summary,{})
