# -*- coding: utf-8 -*-
"""
@author: python273
@contact: https://vk.com/python273
@license Apache License, Version 2.0, see LICENSE file

Copyright (C) 2017
"""

import json

from .base import BaseConfig


class StringConfig(BaseConfig):
    def load(self, filename=None, **kwargs):  # TODO better
        settings = {} if filename is None else json.loads(filename)
        settings.setdefault(self.section_name, {})
        return settings

    def save(self):
        return json.dumps(self._settings)
