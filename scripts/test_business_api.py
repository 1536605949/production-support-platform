import json, os, requests
BASE=os.getenv('BUSINESS_BASE_URL','http://localhost:9000').rstrip('/')
KEY=os.getenv('TOOL_API_KEY','dev-tool-key')
REVIEWER_KEY=os.getenv('REVIEWER_API_KEY','dev-reviewer-key')
H={'X-Tool-API-Key':KEY,'Content-Type':'application/json'}
R={'X-Reviewer-API-Key':REVIEWER_KEY,'Content-Type':'application/json'}

def show(name,r):
    print(f'[{name}] HTTP {r.status_code}')
    try: print(json.dumps(r.json(),ensure_ascii=False,indent=2))
    except Exception: print(r.text)
    return r

r=show('health',requests.get(BASE+'/healthz',timeout=10)); r.raise_for_status()
r=show('order-owned',requests.get(BASE+'/orders/ORD-1001',headers=H,params={'user_id':'user001'},timeout=10)); r.raise_for_status()
r=show('order-cross-user',requests.get(BASE+'/orders/ORD-2001',headers=H,params={'user_id':'user001'},timeout=10)); assert r.status_code==404
r=show('ticket',requests.post(BASE+'/tickets',headers=H,json={'user_id':'user001','issue':'acceptance test ticket'},timeout=10)); r.raise_for_status()

# Credential separation: agent key cannot review, reviewer key cannot query business data.
r=show('tool-key-cannot-review',requests.post(BASE+'/refunds/review',headers=H,json={'user_id':'user001','order_id':'ORD-1001','decision':'approve'},timeout=10)); assert r.status_code==401
r=show('reviewer-key-cannot-query-order',requests.get(BASE+'/orders/ORD-1001',headers=R,params={'user_id':'user001'},timeout=10)); assert r.status_code==401
print('PASS: Business Tool API contract and credential separation')
