#!/usr/bin/env python
# -*- coding:utf-8 -*-
#
# Copyright 2013 cold
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#
#   Author  :   cold
#   E-mail  :   wh_linux@126.com
#   Date    :   13/11/04 10:39:51
#   Desc    :   开启一个Server来处理验证码
#
import os
from tornado.ioloop import IOLoop
from tornado.web import RequestHandler, Application
try:
    from config import HTTP_LISTEN
except ImportError:
    HTTP_LISTEN = "127.0.0.1"

try:
    from config import HTTP_PORT
except ImportError:
    HTTP_PORT = 8000

class CImgHandler(RequestHandler):
    def get(self):
        path = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                            "check.jpg")
        data = ""
        if os.path.exists(path):
            with open(path) as f:
                data = f.read()

        self.set_header("Content-Type", "image/jpeg")
        self.set_header("Content-Length", len(data))
        self.write(data)



class CheckHandler(RequestHandler):
    webqq = None
    r = None
    uin = None
    next_callback = None
    is_login = False

    def get(self):
        path = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                            "check.jpg")
        if not os.path.exists(path):
            html = "暂不需要验证码"
        else:
            html = """
            <img src="/check" />
            <form action="/" method="POST">
                验证码:<input type="text" name="vertify" />
                <input type="submit" name="xx" value="提交" />
            </form>
            """
        self.write(html)

    def post(self):
        code = self.get_argument("vertify")
        code = code.strip().lower().encode('utf-8')
        self.webqq.check_code = code
        pwd = self.webqq.handle_pwd(self.r, code.upper(), self.uin)
        self.next_callback(pwd)
        self.write("已经传递验证码")



app = Application([(r'/', CheckHandler), (r'/check', CImgHandler)])
app.listen(HTTP_PORT, address = HTTP_LISTEN)


def http_server_run(webqq):
    CheckHandler.webqq = webqq
    webqq.get_login_sig(CheckHandler)
    IOLoop.instance().start()
