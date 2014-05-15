#!/usr/bin/env python
# -*- coding:utf-8 -*-
#
#   Author  :   cold
#   E-mail  :   wh_linux@126.com
#   Date    :   14/01/16 12:13:09
#   Desc    :   Õ³Ìù´úÂë²å¼ş
#
from plugins import BasePlugin

class PastePlugin(BasePlugin):
    cont=""
    def is_match(self, from_uin, content, type):
        if type=='g':
            if self.cont==content:
                return True
            self.cont=content
        return False

    def send(self, content, callback):
        """ Ìù´úÂë """
        callback(content)
        self.cont=""

    def handle_message(self, callback):
        self.send(self.cont, callback)
