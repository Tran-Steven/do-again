"""Multi-project visibility must never treat a browser process as a project-ready tab."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from do_again import cli


class ProjectListBrowserReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home=Path(self.tmp.name)
        for project in ("Sonary","jobpipe"):
            repo=home/project
            repo.mkdir()
            metadata=home/".do_again"/"projects"/project/"runtime.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({"repo":str(repo),"browser_enabled":True}))
        self.home=home
        self.projects={"Sonary":str(home/"Sonary"),"jobpipe":str(home/"jobpipe")}

    def run_list(self, target):
        out=io.StringIO()
        def bound(repo):
            return {"chat_url":"https://chatgpt.com/c/"+repo.name}
        def find(port,url):
            return Mock(url=url) if url.endswith("/"+target) else None
        with patch.dict(os.environ,{"DO_AGAIN_HOME":str(self.home/".do_again")}), \
             patch.object(cli,"service_status",return_value={"installed":True,"running":False}), \
             patch.object(cli,"project_record",side_effect=bound), \
             patch.object(cli,"browser_status",return_value={"running":True,"mode":"background","port":9224}), \
             patch.object(cli,"_find_chatgpt_target",side_effect=find) as lookup, \
             contextlib.redirect_stdout(out):
            code=cli.list_projects()
        return code,out.getvalue(),lookup

    def test_one_authenticated_browser_can_have_one_missing_project_tab(self):
        code,output,lookup=self.run_list("jobpipe")
        self.assertEqual(code,0)
        self.assertIn("Sonary\tstopped\ttab-missing",output)
        self.assertIn("jobpipe\tstopped\tbackground",output)
        self.assertEqual(lookup.call_count,2)

    def test_second_project_does_not_inherit_first_project_tab(self):
        code,output,_=self.run_list("Sonary")
        self.assertEqual(code,0)
        self.assertIn("jobpipe\tstopped\ttab-missing",output)
        self.assertIn("Sonary\tstopped\tbackground",output)

    def test_no_mutating_browser_operations_in_readonly_list(self):
        with patch.object(cli,"_find_chatgpt_target",return_value=None), \
             patch.object(cli,"ensure_browser_running") as launch, \
             patch.object(cli.cdp,"create_target") as create, \
             patch.dict(os.environ,{"DO_AGAIN_HOME":str(self.home/".do_again")}), \
             patch.object(cli,"service_status",return_value={"running":False}), \
             patch.object(cli,"project_record",return_value={"chat_url":"https://chatgpt.com/c/existing"}), \
             patch.object(cli,"browser_status",return_value={"running":True,"mode":"background","port":9224}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.list_projects(),0)
        launch.assert_not_called()
        create.assert_not_called()


if __name__=="__main__":
    unittest.main()
