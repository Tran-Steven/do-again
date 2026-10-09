import copy
import unittest
from pathlib import Path
from do_again.supervisor.qualification_agent import validate_binding
from do_again.supervisor.authority import project_identity
from do_again.supervisor.macos_execution import EXECUTION_ROOT,SOCKET_ROOT,ExecutionBlocked


class QualificationBindingTests(unittest.TestCase):
    def setUp(self):
        self.repo=Path('/Users/operator/do-again');key=project_identity(self.repo);nonce='b'*24
        self.root=EXECUTION_ROOT/key/'worker-probes'/nonce
        self.path=self.root/'operator-binding.json'
        self.config={'production_ready':False,'source_sha':'a'*40,'projects':[{'repo':str(self.repo)}]}
        self.value={'source_sha':'a'*40,'nonce':nonce,'repo':str(self.repo),
            'control':str(self.root/'trusted-state/control'),'state':str(self.root/'trusted-state/agent'),
            'policy':str(self.root/'trusted-state/policy.json'),'socket':str(SOCKET_ROOT/('q-'+key[:12]+'-'+nonce+'.sock'))}
    def test_only_fixed_maintenance_binding_can_select_the_fixture_socket(self):
        self.assertEqual(validate_binding(self.value,self.config,self.path)['binding'],self.path)
        for field,value in [('source_sha','c'*40),('nonce','../escape'),('repo','/Users/operator/sonary'),
            ('socket',str(SOCKET_ROOT/'operator.sock')),('control','/tmp/control'),('policy','/tmp/policy')]:
            with self.subTest(field=field):
                candidate=dict(self.value);candidate[field]=value
                with self.assertRaises(ExecutionBlocked):validate_binding(candidate,self.config,self.path)
        with self.assertRaises(ExecutionBlocked):validate_binding(self.value,self.config,self.root/'unowned.json')
    def test_production_configuration_cannot_enable_qualification_entrypoint(self):
        config=copy.deepcopy(self.config);config['production_ready']=True
        with self.assertRaises(ExecutionBlocked):validate_binding(self.value,config,self.path)


if __name__=='__main__':unittest.main()
