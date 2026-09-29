"""Live HITL test against a published Dify v1.7 app.

It sends a refund request in streaming mode, waits for human_input_required,
and optionally submits approve/reject through the Service API form endpoint.
"""
import argparse, json, os, sys, requests

def iter_sse(r):
    for raw in r.iter_lines(decode_unicode=True):
        if raw and raw.startswith('data:'):
            try: yield json.loads(raw[5:].strip())
            except json.JSONDecodeError: pass

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--query',default='请把我的订单 ORD-1001 退款，原因是不需要了')
    ap.add_argument('--user',default='user001')
    ap.add_argument('--action',choices=['approve','reject','none'],default='none')
    args=ap.parse_args()
    base=os.getenv('DIFY_BASE_URL','https://api.dify.ai/v1').rstrip('/')
    key=os.getenv('DIFY_API_KEY','')
    if not key: sys.exit('DIFY_API_KEY is required')
    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}
    payload={'inputs':{},'query':args.query,'response_mode':'streaming','conversation_id':'','user':args.user}
    with requests.post(base+'/chat-messages',headers=headers,json=payload,stream=True,timeout=180) as r:
        print('stream HTTP',r.status_code); r.raise_for_status()
        token=None
        for ev in iter_sse(r):
            print('EVENT',ev.get('event'))
            if ev.get('event')=='human_input_required':
                token=(ev.get('data') or {}).get('form_token')
                print(json.dumps(ev,ensure_ascii=False,indent=2))
                break
    if not token:
        sys.exit('FAIL: no human_input_required event. Confirm v1.7 HITL workflow is published.')
    if args.action=='none':
        print('PASS: HITL pause detected; no action submitted.')
        return
    body={'inputs':{'review_comment':'automated HITL acceptance'},'action':args.action,'user':args.user}
    rr=requests.post(base+f'/form/human_input/{token}',headers=headers,json=body,timeout=60)
    print('submit HTTP',rr.status_code,rr.text[:1000]); rr.raise_for_status()

    # Dify Human Input controls the workflow branch. Persist the reviewer decision through the
    # separate business reviewer credential so the final authorization is not trusted from the LLM.
    business=os.getenv('BUSINESS_BASE_URL','http://localhost:9000').rstrip('/')
    reviewer_key=os.getenv('REVIEWER_API_KEY','dev-reviewer-key')
    review=requests.post(
        business+'/refunds/review',
        headers={'X-Reviewer-API-Key':reviewer_key},
        json={
            'user_id':args.user,
            'order_id':os.getenv('ACCEPTANCE_ORDER','ORD-1001'),
            'decision':args.action,
            'review_comment':'automated HITL acceptance',
        },
        timeout=30,
    )
    print('review persist HTTP',review.status_code,review.text[:1000]); review.raise_for_status()
    print('PASS: HITL action submitted and reviewer decision persisted:',args.action)
if __name__=='__main__': main()
