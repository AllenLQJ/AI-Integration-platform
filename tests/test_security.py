import json
import unittest
import os
import tempfile
from pathlib import Path
from agent_hub.models import redact
from agent_hub.store import Store


class SecurityTests(unittest.TestCase):
    def test_complete_headers_and_json_secrets(self):
        secret = 'SYNTHETIC_SECRET'
        for text in ['Authorization: Bearer '+secret, '{"api_key":"'+secret+'"}',
                     'Cookie: first=one; session='+secret, 'url?token='+secret+'&next=1']:
            self.assertNotIn(secret, redact(text))
        safe = Store.safe({'api_key': secret, 'nested': {'Authorization': 'Bearer '+secret}})
        self.assertNotIn(secret, json.dumps(safe))
    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_existing_explicit_foreign_acl_is_removed(self):
        import win32security, ntsecuritycon
        from agent_hub.config import private_dir
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/'logs').mkdir()
            file = root/'logs'/'old.log'
            file.write_text('synthetic')
            everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
            descriptor = win32security.GetNamedSecurityInfo(str(file), win32security.SE_FILE_OBJECT,
                                                           win32security.DACL_SECURITY_INFORMATION)
            acl = descriptor.GetSecurityDescriptorDacl()
            acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, everyone)
            win32security.SetNamedSecurityInfo(str(file), win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION, None, None, acl, None)
            private_dir(root)
            acl = win32security.GetNamedSecurityInfo(str(file), win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION).GetSecurityDescriptorDacl()
            self.assertFalse(any(acl.GetAce(i)[-1] == everyone for i in range(acl.GetAceCount())))
