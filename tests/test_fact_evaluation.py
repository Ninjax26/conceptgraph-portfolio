"""Authored supplemental PDFs are parsed locally; legacy fixtures stay stable."""
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.services.parser_service import ParserService


class FactEvaluationTests(unittest.TestCase):
    def test_authored_pdf_roundtrip_retains_explicit_facts(self):
        from scripts.evaluate_retrieval_scaling import authored_pdf
        name,content=authored_pdf('facts.pdf',[('SPECIFICATION','Equipment EQ-201. Manufacturer: Atlas Systems. Input voltage: 230 V AC.')])
        pages=ParserService().extract_pages_from_bytes(content)
        self.assertEqual(name,'facts.pdf')
        self.assertIn('Manufacturer: Atlas Systems',pages[0].text)
        self.assertIn('230 V AC',pages[0].text)
        self.assertTrue(content.startswith(b'%PDF'))

    def test_baseline_rerank_uses_matching_baseline_evidence_gate(self):
        import scripts.evaluate_retrieval_scaling as runner
        with TemporaryDirectory() as directory:
            root=Path(directory)
            services=root/'app/services';services.mkdir(parents=True)
            (services/'rerank_service.py').write_text('from app.services.citation_service import prepare_evidence\n')
            (services/'citation_service.py').write_text('def prepare_evidence(*args, **kwargs):\n    return [], {"level": "baseline"}\n')
            env=dict(os.environ,GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_NOSYSTEM='1')
            subprocess.run(['git','init','-q'],cwd=root,env=env,check=True)
            subprocess.run(['git','add','app'],cwd=root,env=env,check=True)
            subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                            '-c','commit.gpgsign=false','commit','-qm','fixture'],cwd=root,env=env,check=True)
            revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
            with patch.object(runner,'ROOT',root):
                modules,hashes=runner.load_baseline_services(revision)
            self.assertIs(modules['rerank_service'].prepare_evidence,modules['citation_service'].prepare_evidence)
            self.assertEqual(modules['rerank_service'].prepare_evidence('question',[])[1]['level'],'baseline')
            self.assertEqual(set(hashes),{'app/services/rerank_service.py','app/services/citation_service.py'})


if __name__=='__main__':
    unittest.main()
