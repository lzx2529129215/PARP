"""Measurement boundaries; no changes to forecast, allocation or reclaim rules."""
import time


def completed_before(rows,cutoff,max_age_s=1.6):
    if any(r['read_finished']<r['window_end'] for r in rows):
        raise ValueError('collection completion precedes collection start')
    eligible=[r for r in rows if r['read_finished']<=cutoff]
    if not eligible:raise ValueError('no fully collected prepeak sample')
    row=max(eligible,key=lambda r:r['read_finished'])
    if cutoff-row['read_finished']>max_age_s:raise ValueError('prepeak sample stale')
    return row


def advance_service(service,now,foreground_directory):
    if service.closed:return
    if now>=service.expires:service.close(now)
    else:service.tick(now,foreground_directory)


def wait_until_ns(deadline,clock=time.monotonic_ns,sleep=time.sleep):
    while True:
        remaining=deadline-clock()
        if remaining<=0:return
        sleep(min(.01,remaining/1e9))


def schedule_diagnostics(operations,tolerance_s=.35):
    """Keep serial-operation queueing as cost; reject late dispatch after readiness."""
    invalid=[];queued=[]
    for op in operations:
        total=op.get('start_lateness_s')
        dispatch=op.get('dispatch_lateness_s',total)
        item=dict(app=op['app'],kind=op['kind'],late_s=total,
                  queue_delay_s=op.get('queue_delay_s',0),dispatch_lateness_s=dispatch)
        if total is not None and total>tolerance_s:queued.append(item)
        if dispatch is not None and dispatch>tolerance_s:invalid.append(item)
    return invalid,queued
