import json

from automation.scripts.report_agentnet_wss import summarize


def test_summary_excludes_partial_and_short_windows(tmp_path):
    def write(name,data):
        (tmp_path/name).write_text(json.dumps(data))
    write('result.json',dict(status='GUI_AUDIT_PASS',task_id='test'))
    write('events.json',[dict(event=name,monotonic_ns=t*10**9) for name,t in [
        ('TASK_PREPARATION_START',35),('OP_START',40),('OP_ACTIONS_RETURNED',65),
        ('POST_IDLE_START',66),('POST_IDLE_END',140)]])
    windows=[]
    for start,end,value,complete,short in [(0,30,20,True,False),(30,60,80,True,False),
            (60,90,999,False,False),(90,120,25,True,False),(120,140,3,True,True)]:
        windows.append(dict(begin_ns=start*10**9,end_ns=end*10**9,referenced_bytes=value*2**20,
            rss_bytes=550*2**20,final_short_window=short,coverage_complete=complete,
            target_window_s=30,collection_gap_s=0,quality_flags=[] if complete else ['process_exit_coverage_loss'],processes=[]))
    (tmp_path/'windows.jsonl').write_text('\n'.join(json.dumps(w) for w in windows))
    summary,*_=summarize(tmp_path)
    assert summary['window_s']==30
    assert summary['operation_overlap_peak_mib']==80
    assert summary['whole_trace_peak_mib']==80
    assert summary['post_idle_median_mib']==25
    assert summary['complete_windows']==4
    assert summary['quality_flags']==['process_exit_coverage_loss']
