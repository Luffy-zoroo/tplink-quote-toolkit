"""合作伙伴平台（组织内部系统）只读访问封装。

要点（2026-10-13 实测）：
- 站点是 Vue SPA，静态资源只部署了 .gz（直连 .js 会 404）→ 需要 .js.gz
- 接口在 /partner/**，POST 多为 JSON body，GET 用 query
- 登录：POST /partner/login（FormData: username=?password=?appLogin=false）
  → Set-Cookie: SAFE_LOGIN_COOKIE_NAME=xxx
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tpkit_config as _cfg  # noqa: E402


def dealer_base():
    """合作伙伴平台根地址。内部系统地址不写死在代码里 —— 从配置项 dealer_base 读取；
    未配置时抛出 _cfg.NotConfigured。"""
    return _cfg.require_base("dealer_base", "合作伙伴平台")


UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


class Dealer:
    def __init__(self, phone=None, password=None):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.ProxyHandler(urllib.request.getproxies()),
            urllib.request.HTTPCookieProcessor(self.cj),
        )
        if phone and password:
            self.login(phone, password)

    # ---------- 底层 ----------
    def _req(self, method, path, data=None, js=None, form=None, params=None):
        url = dealer_base() + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        body = None
        headers = {'User-Agent': UA, 'Accept': 'application/json',
                   'Referer': dealer_base() + '/', 'Origin': dealer_base()}
        if js is not None:
            body = json.dumps(js).encode()
            headers['Content-Type'] = 'application/json;charset=UTF-8'
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif data is not None:
            body = data
        r = urllib.request.Request(url, data=body, headers=headers, method=method)
        last = None
        for attempt in range(4):
            try:
                raw = self.op.open(r, timeout=40).read().decode('utf-8', 'replace')
                break
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (attempt + 1) ** 2)   # 退避：1.5/6/13.5s
                    continue
                raise
            except Exception as e:                          # 网络抖动
                last = e
                time.sleep(1.5 * (attempt + 1) ** 2)
        else:
            raise last
        try:
            return json.loads(raw)
        except Exception:
            return {'_raw': raw[:500]}

    # ---------- 登录 ----------
    def login(self, username, password):
        return self._req('POST', '/partner/login',
                         form={'username': username, 'password': password,
                               'appLogin': 'false'})

    def check_session(self):
        return self._req('GET', '/partner/checkSession')

    # ---------- 产品 ----------
    def brand_category(self, **kw):
        """品牌 / 模块 / 品类树。带 categoryId 时可能返回该品类的规格筛选项。"""
        return self._req('GET', '/partner/product/findBrandCategory', params=kw or None)

    def find_products(self, **kw):
        """产品分页。参数：start limit searchValue brandId firstCategoryId
        secondCategoryId specsFilterList productTag dir collection"""
        p = {'brandId': None, 'dir': 'ASC', 'searchValue': '', 'firstCategoryId': None,
             'limit': 15, 'productTag': None, 'secondCategoryId': None,
             'specsFilterList': [], 'start': 0, 'collection': False}
        p.update(kw)
        return self._req('POST', '/partner/product/findProductPage', js=p)

    def find_by_model(self, model, limit=100, start=0):
        return self._req('POST', '/partner/product/findProductPageByModel',
                         js={'productModel': model, 'limit': limit, 'start': start})

    def detail(self, product_id):
        return self._req('GET', f'/partner/product/productDetail/{product_id}')


if __name__ == '__main__':
    import sys
    d = Dealer(sys.argv[1], sys.argv[2])
    print(json.dumps(d.login(sys.argv[1], sys.argv[2]), ensure_ascii=False))
