import base64, hashlib, hmac, json, os, time, argparse

def b64(x:bytes): return base64.urlsafe_b64encode(x).rstrip(b'=').decode()
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--user',default='user001'); ap.add_argument('--ttl',type=int,default=3600); a=ap.parse_args()
    secret=os.getenv('JWT_SECRET','change-me'); issuer=os.getenv('JWT_ISSUER','dify-support-platform')
    h=b64(json.dumps({'alg':'HS256','typ':'JWT'},separators=(',',':')).encode())
    p=b64(json.dumps({'sub':a.user,'iss':issuer,'iat':int(time.time()),'exp':int(time.time())+a.ttl},separators=(',',':')).encode())
    s=b64(hmac.new(secret.encode(),f'{h}.{p}'.encode(),hashlib.sha256).digest())
    print(f'{h}.{p}.{s}')
if __name__=='__main__': main()
