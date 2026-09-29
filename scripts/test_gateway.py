import argparse, json, os, requests

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--query', default='退款政策是多少？'); ap.add_argument('--user', default='user001'); args=ap.parse_args()
    base=os.getenv('GATEWAY_BASE_URL','http://localhost:8080').rstrip('/')
    r=requests.post(base+'/api/chat', headers={'X-Demo-User':args.user,'Content-Type':'application/json'}, json={'message':args.query}, timeout=130)
    print('HTTP',r.status_code); print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    if r.status_code!=200 or not r.json().get('answer'): raise SystemExit(1)
    print('PASS: gateway -> Dify')
if __name__=='__main__': main()
