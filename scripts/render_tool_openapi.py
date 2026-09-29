import argparse, yaml
from pathlib import Path
ap=argparse.ArgumentParser(); ap.add_argument('--server-url',required=True,help='e.g. https://abc.trycloudflare.com/tool-api'); ap.add_argument('--input',default='tools/business_api.openapi.yaml'); ap.add_argument('--output',default='tools/business_api.bound.openapi.yaml'); a=ap.parse_args()
d=yaml.safe_load(Path(a.input).read_text(encoding='utf-8')); d['servers']=[{'url':a.server_url.rstrip('/')}]; Path(a.output).write_text(yaml.safe_dump(d,allow_unicode=True,sort_keys=False),encoding='utf-8'); print('Wrote',a.output)
