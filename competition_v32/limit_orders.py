"""Bounded testing-only limit BUY lifecycle. Never resubmit an uncertain placement/cancel."""
import json,time

def settle(client,store,intent,plan,response,status,blocked):
    oid=response.get('OrderDetail',{}).get('OrderID')
    if oid is None:raise blocked('Limit ACK lacks order ID')
    started=plan['created_utc'];deadline=started+plan['limit_timeout_seconds']
    cancelled=status in ('CANCEL_SENDING','CANCEL_UNKNOWN','CANCEL_ACK')
    # Query by exact ID. Cancel acknowledgement alone never proves no fill.
    while True:
        q=client.request('/v3/query_order',{'order_id':str(oid)},method='POST',signed=True)
        found=[r for r in q.get('OrderMatched',[]) if str(r.get('OrderID'))==str(oid)] if q.get('Success') is True else []
        if len(found)!=1:raise blocked('Limit order not found; preserve intent')
        r=found[0]
        if r.get('Pair')!=plan['pair'] or r.get('Side')!='BUY' or r.get('Type')!='LIMIT':raise blocked('Limit identity mismatch')
        if r.get('Status') in ('FILLED','CANCELED','CANCELLED','REJECTED','EXPIRED'):
            return r
        if cancelled:raise blocked('Cancellation not terminal; no duplicate write')
        if time.time()>=deadline:
            store.acknowledge(intent,'CANCEL_SENDING',response)
            try:
                result=client.request('/v3/cancel_order',{'order_id':str(oid)},method='POST',signed=True)
            except Exception:
                store.acknowledge(intent,'CANCEL_UNKNOWN',response)
                # The next exact-ID read may resolve a lost response safely.
                cancelled=True;continue
            store.acknowledge(intent,'CANCEL_ACK',response)
            cancelled=True
        else:time.sleep(min(3.1,max(0,deadline-time.time())))
