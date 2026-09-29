import argparse, json, os, sys, requests

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--query', default='退款政策是多少？'); ap.add_argument('--user', default='user001'); args=ap.parse_args()
    base=os.getenv('DIFY_BASE_URL','https://api.dify.ai/v1').rstrip('/'); key=os.getenv('DIFY_API_KEY','')
    if not key: sys.exit('DIFY_API_KEY is required')
    r=requests.post(base+'/chat-messages', headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}, json={'inputs':{},'query':args.query,'response_mode':'blocking','conversation_id':'','user':args.user}, timeout=120)
    print('HTTP',r.status_code); print(json.dumps(r.json() if 'json' in r.headers.get('content-type','') else {'raw':r.text}, ensure_ascii=False, indent=2))
    if r.status_code!=200: raise SystemExit(1)
    data=r.json();
    if not data.get('answer'): raise SystemExit('FAIL: empty answer')
    print('PASS: direct Dify API')
if __name__=='__main__': main()
