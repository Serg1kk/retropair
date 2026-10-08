"""Two-period local contract replay; model outputs are synthetic fixtures, not live inference."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from _util import TempHome, FIXTURES, run_sr
from lib.workspace import ws_paths

class RebrandSmoke(TempHome, unittest.TestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "synthetic-logs"
        self.source.mkdir()
    def cli(self, *args):
        result = run_sr(*args, home=self.home, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result
    def write_json(self, path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False))
        return path
    def quotes(self, obj, found=None):
        found = {} if found is None else found
        if isinstance(obj, dict):
            if all(k in obj for k in ("session_id", "turn_index", "quote")) and obj['turn_index'] is not None:
                found[(obj['session_id'], obj['turn_index'])] = obj['quote']
            for v in obj.values():
                self.quotes(v, found)
        elif isinstance(obj, list):
            for v in obj: self.quotes(v, found)
        return found
    def sessions(self, judge, batch, day):
        quotes = self.quotes(judge)
        self.quotes(batch, quotes)
        for session in judge['data']['session_scope']['sessions']:
            sid = session['session_id']
            rows = []
            for turn in range(session['turns_total']):
                text = quotes.get((sid, turn), f"Synthetic conversation turn {turn}")
                stamp = datetime(2026, 9, day, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=turn)
                rows.append({'type':'user','sessionId':sid,'cwd':str(self.root),
                             'timestamp':stamp.isoformat(),'uuid':f'{sid}-{turn}',
                             'message':{'role':'user','content':text}})
            (self.source / f'{sid}.jsonl').write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in rows)+'\n')
    def test_setup_collect_review_approve_report_next_period(self):
        answers = {'mode':'team','title':'RetroPair smoke','lang':'en','gitignore':True,
                   'people':[{'id':'alex','name':'Synthetic Person','group':'test',
                              'sources':[{'type':'dir','path':str(self.source)}]}]}
        path = self.write_json(self.root/'answers.json', answers)
        self.cli('setup-apply','--answers',str(path))
        paths = ws_paths(self.root)
        batch = json.loads((FIXTURES/'assess/batch-ok.json').read_text())
        first = json.loads((FIXTURES/'assess/judge-alex-p1.json').read_text())
        second = json.loads((FIXTURES/'assess/judge-alex-p2.json').read_text())
        self.sessions(first, batch, 2)
        self.cli('collect')
        index = json.loads(paths['logs_index'].read_text())
        by_id = {e['session_id']:e for e in index['logs']}
        for session in batch['sessions']:
            entry = by_id[session['session_id']]
            session.update(path=entry['path'],turns_total=entry['turns_total'],user_turns=entry['user_turns'],
                           reviewed_from_turn=None,state='new')
        batch['metrics']['sessions_read'] = 2
        path = self.write_json(paths['pending']/'batch.json',batch)
        self.cli('check-collector',str(path))
        for period, judge in [('p1',first),('p2',second)]:
            if period == 'p2':
                self.sessions(second, {}, 8)
                self.cli('collect')
                index = json.loads(paths['logs_index'].read_text())
                by_id = {e['session_id']:e for e in index['logs']}
            for session in judge['data']['session_scope']['sessions']:
                entry = by_id[session['session_id']]
                session.update(path=entry['path'],turns_total=entry['turns_total'],reviewed_from_turn=None,state=entry['state'])
            path = self.write_json(paths['pending']/f'{period}-judge.json',judge)
            self.cli('append',str(path),'--person','alex','--period-label',period)
            before = json.loads(paths['runs'].joinpath('process-reviews.json').read_text())
            self.assertEqual(before['runs'][-1]['review_status'], 'draft')
            self.cli('level',before['runs'][-1]['run_id'])
            self.cli('render-review',str(path),'--person','alex')
            self.cli('approve','alex')
            self.cli('report','--reviewer','--group','--personal','alex')
        tracker = json.loads(paths['runs'].joinpath('process-reviews.json').read_text())
        self.assertEqual(len(tracker['runs']),2)
        self.assertTrue(all(r['review_status'] == 'approved' for r in tracker['runs']))
        self.assertIn('followed',json.dumps(tracker['runs'][-1]))
        personal = paths['reports'].joinpath('personal/alex.html').read_text()
        self.assertIn('retropair',personal.lower())
        self.assertNotIn('sessionretro',personal.lower())
        self.assertIn('Перед тем как принять дифф',personal)
        self.assertIn('followed',personal)
        self.cli('verify')
