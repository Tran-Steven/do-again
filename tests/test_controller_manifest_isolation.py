"""Sandboxed Git manifest must not read the authoritative operator .git."""
import inspect
import unittest
from do_again.supervisor.controller_native_effects import NativeControllerEffects


class ManifestBoundaryTests(unittest.TestCase):
    def test_manifest_uses_sanitized_snapshot_not_protected_git(self):
        source=inspect.getsource(NativeControllerEffects.manifest)
        self.assertIn('copy_git_data(',source)
        self.assertIn("'git-manifest-'",source)
        self.assertIn('item.chmod(0o400)',source)
        self.assertIn('metadata.chmod(0o500)',source)
        self.assertIn('str(metadata),head',source)
        self.assertNotIn("str(broker.project.worktree/'.git'),head",source)
        self.assertIn('with self.admission()',source)
        self.assertIn('stderr_sha256',source)

    def test_protected_source_access_is_broker_only(self):
        source=inspect.getsource(NativeControllerEffects.manifest)
        self.assertIn("copy_git_data(broker.project.worktree/'.git',metadata)",source)
        self.assertIn("head!=self.head()",source)
        self.assertIn('os.chown(metadata,broker.project.uid,broker.project.gid)',source)


if __name__=='__main__':
    unittest.main()
