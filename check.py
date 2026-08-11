from main import fetch_html
from bs4 import BeautifulSoup
import json
html = fetch_html("https://www.myhome.ge/udzravi-qoneba/25601267/qiravdeba-dghiurad-2-otaxiani-bina-saburtaloze/")
soup = BeautifulSoup(html, "html.parser")
script = soup.find("script", id="__NEXT_DATA__")
data = json.loads(script.string)
print(json.dumps(data.get("props", {}).get("pageProps", {}), ensure_ascii=False, indent=2)[:5000])