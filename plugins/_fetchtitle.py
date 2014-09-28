#!/usr/bin/env python
#-*- coding: utf8 -*-
#  Author: lilydjwg (https://github.com/lilydjwg)
#  Source: https://github.com/lilydjwg/winterpy/blob/master/pylib/mytornado/fetchtitle.py
#  由 cold (https://github.com/coldnight) 添加 Python2 支持
# vim:fileencoding=utf-8
import re
import socket

try:
  from urllib.parse import urlsplit, urljoin
  py3 = True
except ImportError:
  from urlparse import urlsplit, urljoin  # py2
  py3 = False

from functools import partial
from collections import namedtuple
import struct
import json
import logging
import encodings.idna
try:
  # Python 3.3
  from html.entities import html5 as entifydefs
except ImportError:
 try:
      from html.entities import entitydefs
 except ImportError:
      from htmlentitydefs import entitydefs

try:
  from html.parser import HTMLParser
except ImportError:    #  py2
  from htmllib import HTMLParser

import tornado.ioloop
import tornado.iostream

# try to import C parser then fallback in pure python parser.
try:
  from http_parser.parser import HttpParser
except ImportError:
  from http_parser.pyparser import HttpParser

UserAgent = 'FetchTitle/1.3 (wh_linux@126.com)'

def get_charset_from_ctype(ctype):
  pos = ctype.find('charset=')
  if pos > 0:
    charset = ctype[pos+8:]
    if charset.lower() == 'gb2312':
      # Windows misleadingly uses gb2312 when it's gbk or gb18030
      charset = 'gb18030'
    elif charset.lower() == 'windows-31j':
      # cp932's IANA name (Windows-31J), extended shift_jis
      # https://en.wikipedia.org/wiki/Code_page_932
      charset = 'cp932'
    return charset

class HtmlTitleParser(HTMLParser):
  charset = title = None
  default_charset = 'utf-8'
  result = None
  _title_coming = False

  def __init__(self):
    # use a list to store literal bytes and escaped Unicode
    self.title = []
    super().__init__()

  def feed(self, bytesdata):
    if bytesdata:
      super().feed(bytesdata.decode('latin1'))
    else:
      self.close()

  def close(self):
    self._check_result(force=True)
    super().close()

  def handle_starttag(self, tag, attrs):
    # Google Search uses wrong meta info
    # Baidu Cache declared charset twice. The former is correct.
    if tag == 'meta' and not self.charset:
      attrs = dict(attrs)
      # try charset attribute first. Wrong quoting may result in this:
      # <META http-equiv=Content-Type content=text/html; charset=gb2312>
      if attrs.get('charset', False):
        self.charset = attrs['charset']
      elif attrs.get('http-equiv', '').lower() == 'content-type':
        self.charset = get_charset_from_ctype(attrs.get('content', ''))
    elif tag == 'title':
      self._title_coming = True

    self._check_result()

  def handle_data(self, data, unicode=False):
    if not unicode:
      data = data.encode('latin1') # encode back
    if self._title_coming:
      self.title.append(data)

  def handle_endtag(self, tag):
    self._title_coming = False
    self._check_result()

  def handle_charref(self, name):
    if name[0] == 'x':
      x = int(name[1:], 16)
    else:
      x = int(name)
    ch = chr(x)
    self.handle_data(ch, unicode=True)

  def handle_entityref(self, name):
    try:
      ch = entitydefs[name]
    except KeyError:
      ch = '&' + name
    self.handle_data(ch, unicode=True)

  def _check_result(self, force=False):
    if self.result is not None:
      return

    if (force or self.charset is not None) \
       and self.title:
      self.result = ''.join(
        x if isinstance(x, str) else x.decode(
          self.charset or self.default_charset,
          errors = 'surrogateescape',
        ) for x in self.title
      )

class SingletonFactory:
  def __init__(self, name):
    self.name = name
  def __repr__(self):
    return '<%s>' % self.name

MediaType = namedtuple('MediaType', 'type size dimension')
defaultMediaType = MediaType('application/octet-stream', None, None)

ConnectionClosed = SingletonFactory('ConnectionClosed')
TooManyRedirection = SingletonFactory('TooManyRedirection')
Timeout = SingletonFactory('Timeout')

logger = logging.getLogger(__name__)

class ContentFinder:
  buf = b''
  def __init__(self, mediatype):
    self._mt = mediatype

  @classmethod
  def match_type(cls, mediatype):
    ctype = mediatype.type.split(';', 1)[0]
    if hasattr(cls, '_mime') and cls._mime == ctype:
      return cls(mediatype)
    if hasattr(cls, '_match_type') and cls._match_type(ctype):
      return cls(mediatype)
    return False

class TitleFinder(ContentFinder):
  parser = None
  pos = 0
  maxpos = 102400 # look at most around 100K

  @staticmethod
  def _match_type(ctype):
    return ctype.find('html') != -1

  def __init__(self, mediatype):
    charset = get_charset_from_ctype(mediatype.type)
    self.parser = HtmlTitleParser()
    self.parser.charset = charset

  def __call__(self, data):
    if data:
      self.pos += len(data)
    if self.pos > self.maxpos:
      # stop here
      data = b''
    self.parser.feed(data)
    if self.parser.result:
      return self.parser.result

class PNGFinder(ContentFinder):
  _mime = 'image/png'
  def __call__(self, data):
    if data is None:
      return self._mt

    self.buf += data
    if len(self.buf) < 24:
      # can't decide yet
      return
    if self.buf[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
      logging.warn('Bad PNG signature and header: %r', self.buf[:16])
      return self._mt._replace(dimension='Bad PNG')
    else:
      s = struct.unpack('!II', self.buf[16:24])
      return self._mt._replace(dimension=s)

class JPEGFinder(ContentFinder):
  _mime = 'image/jpeg'
  isfirst = True
  def __call__(self, data):
    if data is None:
      return self._mt

    # http://www.64lines.com/jpeg-width-height
    if data:
      self.buf += data

    if self.isfirst is True:
      # finding header
      if len(self.buf) < 5:
        return
      if self.buf[:3] != b'\xff\xd8\xff':
        logging.warn('Bad JPEG signature: %r', self.buf[:3])
        return self._mt._replace(dimension='Bad JPEG')
      else:
        self.blocklen = self.buf[4] * 256 + self.buf[5] + 2
        self.buf = self.buf[2:]
        self.isfirst = False

    if self.isfirst is False:
      # receiving a block. 4 is for next block size
      if len(self.buf) < self.blocklen + 4:
        return
      buf = self.buf
      if buf[0] != 0xff:
        logging.warn('Bad JPEG: %r', self.buf[:self.blocklen])
        return self._mt._replace(dimension='Bad JPEG')
      if buf[1] == 0xc0 or buf[1] == 0xc2:
        s = buf[7] * 256 + buf[8], buf[5] * 256 + buf[6]
        return self._mt._replace(dimension=s)
      else:
        # not Start Of Frame, retry with next block
        self.buf = buf = buf[self.blocklen:]
        self.blocklen = buf[2] * 256 + buf[3] + 2
        return self(b'')

class GIFFinder(ContentFinder):
  _mime = 'image/gif'
  def __call__(self, data):
    if data is None:
      return self._mt

    self.buf += data
    if len(self.buf) < 10:
      # can't decide yet
      return
    if self.buf[:3] != b'GIF':
      logging.warn('Bad GIF signature: %r', self.buf[:3])
      return self._mt._replace(dimension='Bad GIF')
    else:
      s = struct.unpack('<HH', self.buf[6:10])
      return self._mt._replace(dimension=s)

class TitleFetcher:
  status_code = 0
  followed_times = 0 # 301, 302
  finder = None
  addr = None
  stream = None
  max_follows = 10
  timeout = 15
  _finished = False
  _cookie = None
  _connected = False
  _redirected_stream = None
  _content_finders = (TitleFinder, PNGFinder, JPEGFinder, GIFFinder)
  _url_finders = ()

  def __init__(self, url, callback,
               timeout=None, max_follows=None, io_loop=None,
               content_finders=None, url_finders=None, referrer=None,
               run_at_init=True,
              ):
    '''
    url: the (full) url to fetch
    callback: called with title or MediaType or an instance of SingletonFactory
    timeout: total time including redirection before giving up
    max_follows: max redirections

    may raise:
    <UnicodeError: label empty or too long> in host preparation
    '''
    self._callback = callback
    self.referrer = referrer
    if max_follows is not None:
      self.max_follows = max_follows

    if timeout is not None:
      self.timeout = timeout
    if hasattr(tornado.ioloop, 'current'):
        default_io_loop = tornado.ioloop.IOLoop.current
    else:
        default_io_loop = tornado.ioloop.IOLoop.instance
    self.io_loop = io_loop or default_io_loop()

    if content_finders is not None:
      self._content_finders = content_finders
    if url_finders is not None:
      self._url_finders = url_finders

    self.origurl = url
    self.url_visited = []
    if run_at_init:
      self.run()

  def run(self):
    if self.url_visited:
      raise Exception("can't run again")
    else:
      self.start_time = self.io_loop.time()
      self._timeout = self.io_loop.add_timeout(
        self.timeout + self.start_time,
        self.on_timeout,
      )
      try:
        self.new_url(self.origurl)
      except:
        self.io_loop.remove_timeout(self._timeout)
        raise

  def on_timeout(self):
    logger.debug('%s: request timed out', self.origurl)
    self.run_callback(Timeout)

  def parse_url(self, url):
    '''parse `url`, set self.host and return address and stream class'''
    self.url = u = urlsplit(url)
    self.host = u.netloc

    if u.scheme == 'http':
      addr = u.hostname, u.port or 80
      stream = tornado.iostream.IOStream
    elif u.scheme == 'https':
      addr = u.hostname, u.port or 443
      stream = tornado.iostream.SSLIOStream
    else:
      raise ValueError('bad url: %r' % url)

    return addr, stream

  def new_connection(self, addr, StreamClass):
    '''set self.addr, self.stream and connect to host'''
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    self.addr = addr
    self.stream = StreamClass(s)
    logger.debug('%s: connecting to %s...', self.origurl, addr)
    self.stream.set_close_callback(self.before_connected)
    self.stream.connect(addr, self.send_request)

  def new_url(self, url):
    self.url_visited.append(url)
    self.fullurl = url

    for finder in self._url_finders:
      f = finder.match_url(url, self)
      if f:
        self.finder = f
        f()
        return

    addr, StreamClass = self.parse_url(url)
    if addr != self.addr:
      if self.stream:
        self.stream.close()
      self.new_connection(addr, StreamClass)
    else:
      logger.debug('%s: try to reuse existing connection to %s', self.origurl, self.addr)
      try:
        self.send_request(nocallback=True)
      except tornado.iostream.StreamClosedError:
        logger.debug('%s: server at %s doesn\'t like keep-alive, will reconnect.', self.origurl, self.addr)
        # The close callback should have already run
        self.stream.close()
        self.new_connection(addr, StreamClass)

  def run_callback(self, arg):
    self.io_loop.remove_timeout(self._timeout)
    self._finished = True
    if self.stream:
      self.stream.close()
    self._callback(arg, self)

  def send_request(self, nocallback=False):
    self._connected = True
    req = ['GET %s HTTP/1.1',
           'Host: %s',
           # t.co will return 200 and use js/meta to redirect using the following :-(
           # 'User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:16.0) Gecko/20100101 Firefox/16.0',
           'User-Agent: %s' % UserAgent,
           'Accept: text/html,application/xhtml+xml;q=0.9,*/*;q=0.7',
           'Accept-Language: zh-cn,zh;q=0.7,en;q=0.3',
           'Accept-Charset: utf-8,gb18030;q=0.7,*;q=0.7',
           'Accept-Encoding: gzip, deflate',
           'Connection: keep-alive',
          ]
    if self.referrer is not None:
      req.append('Referer: ' + self.referrer.replace('%', '%%'))
    path = self.url.path or '/'
    if self.url.query:
      path += '?' + self.url.query
    req = '\r\n'.join(req) % (
      path, self._prepare_host(self.host),
    )
    if self._cookie:
      req += '\r\n' + self._cookie
    req += '\r\n\r\n'
    self.stream.write(req.encode())
    self.headers_done = False
    self.parser = HttpParser(decompress=True)
    if not nocallback:
      self.stream.read_until_close(
        # self.addr and self.stream may have been changed when close callback is run
        partial(self.on_data, close=True, addr=self.addr, stream=self.stream),
        streaming_callback=self.on_data,
      )

  def _prepare_host(self, host):
    host = encodings.idna.nameprep(host)
    return b'.'.join(encodings.idna.ToASCII(x) if x else b''
                     for x in host.split('.')).decode('ascii')

  def on_data(self, data, close=False, addr=None, stream=None):
    if close:
      logger.debug('%s: connection to %s closed.', self.origurl, addr)

    if self.stream.error:
      self.run_callback(self.stream.error)
      return

    if (close and stream and self._redirected_stream is stream) or self._finished:
      # The connection is closing, and we are being redirected or we're done.
      self._redirected_stream = None
      return

    recved = len(data)
    logger.debug('%s: received data: %d bytes', self.origurl, recved)

    p = self.parser
    nparsed = p.execute(data, recved)
    if close:
      # feed EOF
      p.execute(b'', 0)

    if not self.headers_done and p.is_headers_complete():
      if not self.on_headers_done():
        return

    if p.is_partial_body():
      chunk = p.recv_body()
      if self.finder is None:
        # redirected but has body received
        return
      t = self.feed_finder(chunk)
      if t is not None:
        self.run_callback(t)
        return

    if p.is_message_complete():
      if self.finder is None:
        # redirected but has body received
        return
      t = self.feed_finder(None)
      # if title not found, t is None
      self.run_callback(t)
    elif close:
      self.run_callback(self.stream.error or ConnectionClosed)

  def before_connected(self):
    '''check if something wrong before connected'''
    if not self._connected and not self._finished:
      self.run_callback(self.stream.error)

  def process_cookie(self):
    setcookie = self.headers.get('Set-Cookie', None)
    if not setcookie:
      return

    cookies = [c.rsplit(None, 1)[-1] for c in setcookie.split('; expires')[:-1]]
    self._cookie = 'Cookie: ' + '; '.join(cookies)

  def on_headers_done(self):
    '''returns True if should proceed, None if should stop for current chunk'''
    self.headers_done = True
    self.headers = self.parser.get_headers()

    self.status_code = self.parser.get_status_code()
    if self.status_code in (301, 302):
      self.process_cookie() # or we may be redirecting to a loop
      logger.debug('%s: redirect to %s', self.origurl, self.headers['Location'])
      self.followed_times += 1
      if self.followed_times > self.max_follows:
        self.run_callback(TooManyRedirection)
      else:
        newurl = urljoin(self.fullurl, self.headers['Location'])
        self._redirected_stream = self.stream
        self.new_url(newurl)
      return

    try:
      l = int(self.headers.get('Content-Length', None))
    except (ValueError, TypeError):
      l = None

    ctype = self.headers.get('Content-Type', 'text/html')
    mt = defaultMediaType._replace(type=ctype, size=l)
    for finder in self._content_finders:
      f = finder.match_type(mt)
      if f:
        self.finder = f
        break
    else:
      self.run_callback(mt)
      return

    return True

  def feed_finder(self, chunk):
    '''feed data to finder, return the title if found'''
    t = self.finder(chunk)
    if t is not None:
      return t

class URLFinder:
  def __init__(self, url, fetcher, match=None):
    self.fullurl = url
    self.match = match
    self.fetcher = fetcher

  @classmethod
  def match_url(cls, url, fetcher):
    if hasattr(cls, '_url_pat'):
      m = cls._url_pat.match(url)
      if m is not None:
        return cls(url, fetcher, m)
    if hasattr(cls, '_match_url') and cls._match_url(url, fetcher):
      return cls(url, fetcher)

  def done(self, info):
    self.fetcher.run_callback(info)

class GithubFinder(URLFinder):
  _url_pat = re.compile(r'https://github\.com/(?!blog/)(?P<repo_path>[^/]+/[^/]+)/?$')
  _api_pat = 'https://api.github.com/repos/{repo_path}'
  httpclient = None

  def __call__(self):
    if self.httpclient is None:
      from tornado.httpclient import AsyncHTTPClient
      httpclient = AsyncHTTPClient()
    else:
      httpclient = self.httpclient

    m = self.match
    httpclient.fetch(self._api_pat.format(**m.groupdict()), self.parse_info,
                     headers={
                       'User-Agent': UserAgent,
                     })

  def parse_info(self, res):
    if res.error:
      self.done(res.error)
      return
    repoinfo = json.loads(res.body.decode('utf-8'))
    self.response = res
    self.done(repoinfo)

class GithubUserFinder(GithubFinder):
  _url_pat = re.compile(r'https://github\.com/(?!blog(?:$|/))(?P<user>[^/]+)/?$')
  _api_pat = 'https://api.github.com/users/{user}'

def main(urls, url_finders=(GithubFinder,)):
  class BatchFetcher:
    n = 0
    def __call__(self, title, fetcher):
      if isinstance(title, bytes):
        try:
          title = title.decode('gb18030')
        except UnicodeDecodeError:
          pass
      url = ' <- '.join(reversed(fetcher.url_visited))
      logger.info('done: [%d] %s <- %s' % (fetcher.status_code, title, url))
      self.n -= 1
      if not self.n:
        tornado.ioloop.IOLoop.instance().stop()

    def add(self, url):
      TitleFetcher(url, self, url_finders=url_finders)
      self.n += 1

  from myutils import enable_pretty_logging
  enable_pretty_logging()
  f = BatchFetcher()
  for u in urls:
    f.add(u)
  tornado.ioloop.IOLoop.instance().start()

def test():
  urls = (
    'http://lilydjwg.is-programmer.com/',
    'http://www.baidu.com',
    'https://zh.wikipedia.org', # redirection
    'http://redis.io/',
    'http://lilydjwg.is-programmer.com/2012/10/27/streaming-gzip-decompression-in-python.36130.html', # maybe timeout
    'http://img.vim-cn.com/22/cd42b4c776c588b6e69051a22e42dabf28f436', # image with length
    'https://github.com/m13253/titlebot/blob/master/titlebot.py_', # 404
    'http://lilydjwg.is-programmer.com/admin', # redirection
    'http://twitter.com', # connect timeout
    'http://www.wordpress.com', # reset
    'http://jquery-api-zh-cn.googlecode.com/svn/trunk/xml/jqueryapi.xml', # xml
    'http://lilydjwg.is-programmer.com/user_files/lilydjwg/config/avatar.png', # PNG
    'http://img01.taobaocdn.com/bao/uploaded/i1/110928240/T2okG7XaRbXXXXXXXX_!!110928240.jpg', # JPEG with Start Of Frame as the second block
    'http://file3.u148.net/2013/1/images/1357536246993.jpg', # JPEG that failed previous code
    'http://gouwu.hao123.com/', # HTML5 GBK encoding
    'https://github.com/lilydjwg/winterpy', # github url finder
    'http://github.com/lilydjwg/winterpy', # github url finder with redirect
    'http://导航.中国/', # Punycode. This should not be redirected
    'http://t.cn/zTOgr1n', # multiple redirections
    'http://www.galago-project.org/specs/notification/0.9/x408.html', # </TITLE\n>
    'http://x.co/dreamz', # redirection caused false ConnectionClosed error
    # http_parser won't decode this big gzip?
    'http://m8y.org/tmp/zipbomb/zipbomb_light_nonzero.html', # very long title
    'http://www.83wyt.com', # reversed meta attribute order
    'https://www.inoreader.com', # malformed start tag: <meta http-equiv="Content-Type" content="text/html" ; charset="UTF-8">
    'https://linuxtoy.org/archives/linux-deepin-2014-alpha-into-new-deepin-world.html', # charref outside ASCII
    'http://74.125.235.191/search?site=&source=hp&q=%E6%9C%8D%E5%8A%A1%E5%99%A8+SSD&btnG=Google+%E6%90%9C%E7%B4%A2', # right charset in HTTP, wrong in HTML
    'http://digital.sina.com.hk/news/-7-1514837/1.html', # mixed Big5 and non-Big5 escaped Unicode character
    'http://cache.baiducontent.com/c?m=9f65cb4a8c8507ed4fece7631046893b4c4380147c808c5528888448e435061e5a27b9e867750d04d6c57f6102ad4b57f7fa3372340126bc9fcc825e98e6d27e20d77465671df65663a70edecb5124b137e65ffed86ef0bb8025e3ddc5a2de4352ba44757d97818d4d0164dd1efa034093b1e842022e60adec40728f2d6058e93430c6508ae5256f779686d94b3db3&p=882a9e41c0d25ffc57efdc394c52&newp=8a64865b85cc43ff57e6902c495f92695803ed603fd3d7&user=baidu&fm=sc&query=mac%CF%C2%D7%EE%BA%C3%B5%C4%C8%CB%C8%CB%BF%CD%BB%A7%B6%CB&qid=&p1=5', # HTML document inside another, correct charset is in outside one and title inside
  )
  main(urls)

if __name__ == "__main__":
  import sys
  try:
    if len(sys.argv) == 1:
      sys.exit('no urls given.')
    elif sys.argv[1] == 'test':
      test()
    else:
      main(sys.argv[1:])
  except KeyboardInterrupt:
    print('Interrupted.')
