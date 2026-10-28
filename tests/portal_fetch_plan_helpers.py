"""Pure plan delta on copies. No installation or execution authority."""
from copy import deepcopy
import re

APPROVAL = '/synthetic/portal-fixture/.flock-portal-intake/approval.json'
EGRESS = '/synthetic/portal-fixture/.flock-portal-intake/egress.json'
PROJECT = '/synthetic/portal-fixture/records-project'


def portal_plan(base, *, release_path=None, apply=False, max_items=20, max_bytes_total=524288000):
    from campaign_tool.records.portal.policy import fetch_settings
    if not isinstance(base,dict) or base.get('target') != 'LIVE' or base.get('mode') != 'live':
        raise ValueError('plan_shape')
    settings={'apply':apply,'max_items':max_items,'max_bytes_total':max_bytes_total}
    fetch_settings({'portal':{'fetch':settings}})
    stages=base.get('stages')
    if not isinstance(stages,list) or any(not isinstance(s,dict) for s in stages):
        raise ValueError('plan_shape')
    positions=[i for i,s in enumerate(stages) if s.get('name')=='portal']
    if len(positions)!=1:
        raise ValueError('portal_stage_identity')
    current=stages[positions[0]]
    if set(current) != {'name','argv','timeout_seconds','on_fail'} or not isinstance(current['argv'],list):
        raise ValueError('unreviewed_portal_stage_shape')
    argv=current['argv']
    for flag,value in (('--approval-file',APPROVAL),('--project',PROJECT)):
        if argv.count(flag)!=1 or argv.index(flag)+1>=len(argv) or argv[argv.index(flag)+1]!=value:
            raise ValueError('unreviewed_portal_binding')
    reserved=('--portal-download','--portal-max-items','--portal-total-bytes','--portal-egress-file')
    if argv.count('campaign_tool.records.run_stages')!=1 or any(a.split('=',1)[0] in reserved for a in argv):
        raise ValueError('unreviewed_portal_binding')
    result=deepcopy(base)
    result['portal']={'fetch':settings}
    if not apply:
        return result
    if not isinstance(release_path,str) or not re.fullmatch(r'/synthetic/portal-fixture/engine-portal-[a-f0-9]{64}',release_path):
        raise ValueError('reviewed_release_required')
    stage=result['stages'][positions[0]]
    # Existing invocation format is data only. It requires attended guard-owner admission.
    pointers=[i for i,a in enumerate(argv) if a.startswith('PYTHONPATH=')]
    if len(pointers)!=1 or argv.count('--timeout')!=1:
        raise ValueError('unreviewed_portal_binding')
    stage['argv'][pointers[0]]='PYTHONPATH='+release_path
    stage['argv'][argv.index('--timeout')+1]='1190'
    stage['argv']+=['--portal-egress-file',EGRESS]
    stage.update(timeout_seconds=1200,on_fail='stop')
    return result
