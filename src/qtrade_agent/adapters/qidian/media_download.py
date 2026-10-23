"""Fetch only an original image URL contained in a typed native PicRec.
No login cookies, relay credentials, or arbitrary-host/redirect requests are used.
"""
import urllib.request
import urllib.error
from urllib.parse import urlsplit, urlunsplit

_HOSTS = frozenset(('gchat.qpic.cn', 'c2cpicdw.qpic.cn', 'c2cpicdw.qpic.cn', 'multimedia.nt.qq.com'))
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

def validated_original_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 for c in value):
        raise ValueError('NATIVE_MEDIA_URL_INVALID')
    try:
        u = urlsplit(value)
        if u.scheme not in ('http','https') or u.hostname not in _HOSTS or u.port not in (None,80,443) or u.username or u.password or u.fragment:
            raise ValueError()
        if not u.path.startswith(('/gchatpic_new/','/offpic_new/','/download')): raise ValueError()
        # Preserve signed path/query; only upgrade cleartext transport, never downgrade TLS.
        return urlunsplit(('https',u.hostname,u.path,u.query,''))
    except ValueError: raise ValueError('NATIVE_MEDIA_URL_NOT_ALLOWED') from None

class OriginalImageDownloader:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    def __call__(self, reference):
        if not reference.md5 or reference.encrypted:
            raise ValueError('NATIVE_MEDIA_REMOTE_IDENTITY_REQUIRED')
        url = validated_original_url(reference.raw_url)
        limit = reference.size if reference.size > 0 else 50*1024*1024
        if limit > 50*1024*1024: raise ValueError('NATIVE_MEDIA_SIZE_INVALID')
        try:
            with self.opener.open(urllib.request.Request(url, headers={'Accept-Encoding':'identity'}), timeout=15) as response:
                if response.status != 200: raise ValueError('NATIVE_MEDIA_DOWNLOAD_REJECTED')
                size = response.headers.get('Content-Length')
                if size and (not size.isdecimal() or int(size)>limit): raise ValueError('NATIVE_MEDIA_SIZE_INVALID')
                raw = response.read(limit+1)
        except (OSError, urllib.error.URLError):
            raise ValueError('NATIVE_MEDIA_DOWNLOAD_UNAVAILABLE') from None
        if len(raw)>limit: raise ValueError('NATIVE_MEDIA_SIZE_INVALID')
        return raw