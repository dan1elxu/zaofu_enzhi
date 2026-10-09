from pathlib import Path
import pytest
from tests import test_channel_discussion_driver as helpers
from zf.runtime.channel_discussion import advance_discussion
from zf.runtime.channel_projection import project_channel
h=vars(helpers)
def test_old_rejection_cannot_reopen_phase2_while_revised_prd_is_generating(tmp_path):
 state,writer=h['_run_to_phase3'](tmp_path);ch=h['CH']
 first=project_channel(state,ch)['synthesis_requests'][-1]['request_id']
 writer.emit('channel.synthesis.proposed',actor='pm-1',payload={'channel_id':ch,'thread_id':'main','request_id':first,'artifact_ref':'prd/r1.json','artifact_digest':'old','prd_revision':1})
 h['_consensus'](writer,'proposed','pm-1',artifact_ref='prd/r1.json',artifact_digest='old',prd_revision=1,proposed_by='pm-1')
 h['_consensus'](writer,'blocked','critic-1',member_id='critic-1',artifact_ref='prd/r1.json',artifact_digest='old',blocker_question_id='q-blocker',blocker_question='fix evidence?')
 advance_discussion(state,writer,channel_id=ch,thread_id='main')
 h['_consensus'](writer,'blocked','arch-1',member_id='arch-1',artifact_ref='prd/r1.json',artifact_digest='old',blocker_question_id='q-late',blocker_question='late evidence?')
 advance_discussion(state,writer,channel_id=ch,thread_id='main')
 h['_resolve'](writer,'q-late',resolution='answered',actor='operator')
 h['_resolve'](writer,'q-blocker',resolution='answered',actor='operator')
 advance_discussion(state,writer,channel_id=ch,thread_id='main')
 assert project_channel(state,ch)['discussions']['main']['state']=='phase3_synthesis'
 for _ in range(2):advance_discussion(state,writer,channel_id=ch,thread_id='main')
 detail=project_channel(state,ch)
 assert detail['discussions']['main']['state']=='phase3_synthesis'
 assert len(detail['synthesis_requests'])==2


@pytest.mark.parametrize("block", ["", "missing_signature", "missing_owner", "open_question", "blocked", "stalled"])
def test_sweep_settles_only_fully_approved_current_revision_after_prior_close(tmp_path: Path, block):
 from zf.runtime.channel_discussion import sweep_discussion_deadlines
 from zf.core.events import EventLog
 state,writer=h['_run_to_phase3'](tmp_path);ch=h['CH']
 writer.emit('channel.discussion.closed',actor='test',payload={'channel_id':ch,'thread_id':'main','outcome':'stalled' if block=='stalled' else 'consensus'})
 h['_consensus'](writer,'proposed','pm-1',artifact_ref='new-prd.md',proposed_by='pm-1')
 for member in ['pm-1','arch-1','critic-1']:
  if block!='missing_signature' or member!='critic-1':h['_consensus'](writer,'signed',member,member_id=member,artifact_ref='new-prd.md')
 if block!='missing_owner':h['_consensus'](writer,'signed','operator',member_id='operator',artifact_ref='new-prd.md')
 if block=='open_question':h['_open_question'](writer,'q-open',question='New unresolved product decision')
 if block=='blocked':h['_consensus'](writer,'blocked','critic-1',member_id='critic-1',artifact_ref='new-prd.md',blocker_question_id='q-blocked')
 sweep_discussion_deadlines(state,writer)
 sweep_discussion_deadlines(state,writer)
 events=[e for e in EventLog(state/'events.jsonl').read_all() if e.type=='channel.consensus.reached']
 assert len(events)==(0 if block else 1)
 if events:assert events[0].payload['artifact_ref']=='new-prd.md'
