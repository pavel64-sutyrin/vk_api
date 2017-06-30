﻿# -*- coding: utf-8 -*-
"""
@author: Kirill Python
@contact: https://vk.com/python273
@license Apache License, Version 2.0, see LICENSE file

Copyright (C) 2017
"""

import logging
import re
import threading
import time

import requests

import jconfig
from .exceptions import *
from .utils import code_from_number, search_re, clean_string

DELAY = 0.34  # ~3 requests per second
TOO_MANY_RPS_CODE = 6
NEED_VALIDATION_CODE = 17
HTTP_ERROR_CODE = -1
TWOFACTOR_CODE = -2

RE_LOGIN_HASH = re.compile(r'name="lg_h" value="([a-z0-9]+)"')
RE_CAPTCHAID = re.compile(r"onLoginCaptcha\('(\d+)'")
RE_NUMBER_HASH = re.compile(r"al_page: '3', hash: '([a-z0-9]+)'")
RE_AUTH_HASH = re.compile(
    r"\{.*?act: 'a_authcheck_code'.+?hash: '([a-z_0-9]+)'.*?\}"
)
RE_TOKEN_URL = re.compile(r'location\.href = "(.*?)"\+addr;')

RE_PHONE_PREFIX = re.compile(r'label ta_r">\+(.*?)<')
RE_PHONE_POSTFIX = re.compile(r'phone_postfix">.*?(\d+).*?<')


class VkApi(object):
    def __init__(self, login=None, password=None, token=None,
                 auth_handler=None, captcha_handler=None,
                 proxies=None,
                 config=jconfig.Config, config_filename='vk_config.v2.json',
                 api_version='5.63', app_id=2895443, scope=33554431,
                 client_secret=None):
        """
        :param login: Логин ВКонтакте (лучше использовать номер телефона для
                       автоматического обхода проверки безопасности)
        :param password: Пароль ВКонтакте (если пароль не передан, то будет
                          попытка использовать сохраненные данные для
                          аутентификации)

        :param token: access_token
        :param auth_handler: Функция для обработки двухфакторной аутентификации,
                              должна возвращать строку с кодом и
                              булевое значение, означающее, стоит ли запомнить
                              это устройство, для прохождения аутентификации.
        :param captcha_handler: Функция для обработки капчи
        :param config: класс для сохранения настроек
        :param config_filename: Расположение config файла

        :param api_version: Версия API
        :param app_id: Standalone-приложение
        :param scope: Запрашиваемые права. Можно передать строкой
        :param client_secret: Защищенный ключ приложения для серверной
                               авторизации (https://vk.com/dev/auth_server)
        """

        self.login = login
        self.password = password

        self.sid = None
        self.token = {'access_token': token}

        self.api_version = api_version
        self.app_id = app_id
        self.scope = scope
        self.client_secret = client_secret

        self.settings = config(self.login, filename=config_filename)

        self.http = requests.Session()
        self.http.proxies = proxies
        self.http.headers.update({
            'User-agent': 'Mozilla/5.0 (Windows NT 6.1; rv:40.0) '
            'Gecko/20100101 Firefox/40.0'
        })

        self.last_request = 0.0

        self.error_handlers = {
            NEED_VALIDATION_CODE: self.need_validation_handler,
            CAPTCHA_ERROR_CODE: captcha_handler or self.captcha_handler,
            TOO_MANY_RPS_CODE: self.too_many_rps_handler,
            TWOFACTOR_CODE: auth_handler or self.auth_handler
        }

        self.lock = threading.Lock()

        self.logger = logging.getLogger('vk_api')

    def auth(self, reauth=False, token_only=False):
        """ Аутентификация

        :param reauth: Позволяет переавторизиваться, игнорируя сохраненные
                       куки и токен

        :param token_only: Включает оптимальную стратегию аутентификации, если
                            необходим только access_token

                            Например если сохраненные куки не валидны,
                            но токен валиден, то аутентификация пройдет успешно

                            При token_only=False, сначала проверяется
                            валидность куки. Если кука не будет валидна, то
                            будет произведена попытка аутетификации с паролем.
                            Тогда если пароль не верен или пароль не передан,
                            то аутентификация закончится с ошибкой.

                            Если вы не делаете запросы к веб версии сайта
                            используя куки, то лучше использовать
                            token_only=True
        """

        if not self.login:
            self.logger.info('No login to auth')
            return

        self.logger.info('Auth with login: {}'.format(self.login))

        self.sid = self.settings.remixsid
        self.token = self.settings.setdefault('token', {}).get(str(self.scope))

        if not token_only:
            self._auth_cookies(reauth=reauth)
        else:
            self._auth_token(reauth=reauth)

    def _auth_cookies(self, reauth=False):

        if reauth:
            self.logger.info('Auth forced')

            self.settings.clear_section()

            self.vk_login()
            self.api_login()
            return

        if not self.check_sid():
            self.logger.info(
                'remixsid from config is not valid: {}'.format(
                    self.sid
                )
            )

            self.vk_login()
        else:
            self.security_check()

        if not self.check_token():
            self.logger.info(
                'access_token from config is not valid: {}'.format(
                    self.token
                )
            )

            self.api_login()
        else:
            self.logger.info('access_token from config is valid')

    def _auth_token(self, reauth=False):

        if not reauth and self.check_token():
            self.logger.info('access_token from config is valid')
            return

        if reauth:
            self.logger.info('Auth (API) forced')

        if self.check_sid():
            self.security_check()
            self.api_login()

        elif self.password:
            self.vk_login()
            self.api_login()

    def authorization(self, *args, **kwargs):
        import warnings
        warnings.simplefilter('always', DeprecationWarning)
        warnings.warn(
            'Please replace `VkApi.authorization` with `VkApi.auth` and '
            '`AuthorizationError` with `AuthError`',
            DeprecationWarning
        )

        return self.auth(*args, **kwargs)

    def vk_login(self, captcha_sid=None, captcha_key=None):
        """ Авторизация ВКонтакте с получением cookies remixsid """

        self.logger.info('Logging in...')

        if not self.password:
            self.logger.info('No password')
            raise PasswordRequired('Password is required to login')

        self.http.cookies.clear()

        # Get cookies
        response = self.http.get('https://vk.com/')

        values = {
            'act': 'login',
            'role': 'al_frame',
            '_origin': 'https://vk.com',
            'utf8': '1',
            'email': self.login,
            'pass': self.password,
            'lg_h': search_re(RE_LOGIN_HASH, response.text)
        }

        if captcha_sid and captcha_key:
            self.logger.info(
                'Using captcha code: {}: {}'.format(
                    captcha_sid,
                    captcha_key
                )
            )

            values.update({
                'captcha_sid': captcha_sid,
                'captcha_key': captcha_key
            })

        response = self.http.post('https://login.vk.com/', values)

        if 'onLoginCaptcha(' in response.text:
            self.logger.info('Captcha code is required')

            captcha_sid = search_re(RE_CAPTCHAID, response.text)
            captcha = Captcha(self, captcha_sid, self.vk_login)

            return self.error_handlers[CAPTCHA_ERROR_CODE](captcha)

        if 'onLoginFailed(4' in response.text:
            self.logger.info('Bad password')
            raise BadPassword('Bad password')

        if 'act=authcheck' in response.text:
            self.logger.info('Two factor is required')

            response = self.http.get('https://vk.com/login?act=authcheck')

            self.twofactor(response)

        remixsid = (
            self.http.cookies.get('remixsid') or
            self.http.cookies.get('remixsid6')
        )

        if remixsid:
            self.logger.info('Got remixsid')

            self.settings.remixsid = remixsid

            # Нужно для авторизации в API
            self.settings.forapilogin = {
                'p': self.http.cookies['p'],
                'l': self.http.cookies['l']
            }

            self.settings.save()

            self.sid = remixsid
        else:
            self.logger.info('Unknown auth error')

            raise AuthError(
                'Unknown error. Please send bugreport: https://vk.com/python273'
            )

        response = self.security_check()

        if 'act=blocked' in response.url:
            raise AccountBlocked('Account is blocked')

    def twofactor(self, auth_response):
        """ Двухфакторная аутентификация
            :param auth_response: страница с приглашением к аутентификации
        """
        code, remember_device = self.error_handlers[TWOFACTOR_CODE]()

        auth_hash = search_re(RE_AUTH_HASH, auth_response.text)

        values = {
            'act': 'a_authcheck_code',
            'al': '1',
            'code': code,
            'remember': int(remember_device),
            'hash': auth_hash,
        }

        response = self.http.post('https://vk.com/al_login.php', values)
        response_parsed = response.text.split('<!>')

        if response_parsed[4] == '4':  # OK
            return self.http.get('https://vk.com/' + response_parsed[5])

        elif response_parsed[4] == '8':  # Incorrect code
            return self.twofactor(auth_response)

        raise TwoFactorError('Two factor authentication failed')

    def security_check(self, response=None):
        self.logger.info('Checking security check request')

        if response is None:
            response = self.http.get('https://vk.com/settings')
            if 'security_check' not in response.url:
                self.logger.info('Security check is not required')
                return response

        phone_prefix = clean_string(search_re(RE_PHONE_PREFIX, response.text))
        phone_postfix = clean_string(search_re(RE_PHONE_POSTFIX, response.text))

        code = None
        if self.login:
            code = code_from_number(phone_prefix, phone_postfix, self.login)

        if code:
            number_hash = search_re(RE_NUMBER_HASH, response.text)

            values = {
                'act': 'security_check',
                'al': '1',
                'al_page': '3',
                'code': code,
                'hash': number_hash,
                'to': ''
            }

            response = self.http.post('https://vk.com/login.php', values)

            if response.text.split('<!>')[4] == '4':
                return response

        if phone_prefix and phone_postfix:
            raise SecurityCheck(phone_prefix, phone_postfix)

        raise SecurityCheck(response=response)

    def check_sid(self):
        """ Проверка Cookies remixsid на валидность """

        self.logger.info('Checking remixsid...')

        if not self.sid:
            self.logger.info('No remixsid')
            return

        url = 'https://vk.com/feed2.php'
        self.http.cookies.update({
            'remixsid': self.sid,
            'remixlang': '0',
            'remixsslsid': '1'
        })

        response = self.http.get(url).json()

        if response['user']['id'] != -1:
            self.logger.info('remixsid is valid')
            return response

        self.logger.info('remixsid is not valid')

    def api_login(self):
        """ Получение токена через Desktop приложение """

        if not self.sid or not self.settings.forapilogin:
            raise AuthError('API authorization error (no cookies)')

        url = 'https://oauth.vk.com/authorize'
        values = {
            'client_id': self.app_id,
            'scope': self.scope,
            'response_type': 'token',
        }

        self.http.cookies.update(self.settings.forapilogin)
        self.http.cookies.update({'remixsid': self.sid})

        response = self.http.get(url, params=values)

        if 'access_token' not in response.url:
            url = search_re(RE_TOKEN_URL, response.text)

            if url:
                response = self.http.get(url)

        if 'access_token' in response.url:
            params = response.url.split('#')[1].split('&')

            token = {}
            for i in params:
                x = i.split('=')
                token.update({x[0]: x[1]})

            self.settings.setdefault('token', {})[str(self.scope)] = token
            self.settings.save()
            self.token = token

            self.logger.info('Got access_token')
        else:
            raise AuthError('Authorization error (api)')

    def server_auth(self):
        """ Серверная авторизация """
        values = {
            'client_id': self.app_id,
            'client_secret': self.client_secret,
            'v': self.api_version,
            'grant_type': 'client_credentials'
        }

        response = self.http.post(
            'https://oauth.vk.com/access_token', values).json()

        if 'error' in response:
            raise AuthError(response['error_description'])
        else:
            self.token = response

    def check_token(self):
        """ Проверка access_token на валидность """

        if self.token:
            try:
                self.method('stats.trackVisitor')
            except ApiError:
                return False

            return True

    def captcha_handler(self, captcha):
        """ http://vk.com/dev/captcha_error """
        raise captcha

    def need_validation_handler(self, error):
        """ http://vk.com/dev/need_validation """
        # TODO: write me
        pass

    def http_handler(self, error):
        """ Handle connection errors """
        pass

    def too_many_rps_handler(self, error):
        time.sleep(0.5)
        return error.try_method()

    def auth_handler(self):
        raise AuthError('No handler for two-factor authentication')

    def get_api(self):
        return VkApiMethod(self)

    def method(self, method, values=None, captcha_sid=None, captcha_key=None, raw=False):
        """ Использование методов API

        :param method: метод
        :param values: параметры
        :param captcha_sid:
        :param captcha_key:
        :param raw: при False возвращает response['response'], при True возвращает response
                    e.g. может понадобиться для метода execute для получения execute_errors
        """

        url = 'https://api.vk.com/method/%s' % method
        values = values.copy() if values else {}

        if 'v' not in values:
            values['v'] = self.api_version

        if self.token:
            values['access_token'] = self.token['access_token']

        if captcha_sid and captcha_key:
            values['captcha_sid'] = captcha_sid
            values['captcha_key'] = captcha_key

        with self.lock:
            # Ограничение 3 запроса в секунду
            delay = DELAY - (time.time() - self.last_request)

            if delay > 0:
                time.sleep(delay)

            response = self.http.post(url, values)
            self.last_request = time.time()

        if response.ok:
            response = response.json()
        else:
            error = ApiHttpError(self, method, values, response)
            response = self.http_handler(error)

            if response is not None:
                return response

            raise error

        if 'error' in response:
            error = ApiError(self, method, values, response['error'])

            if error.code in self.error_handlers:
                if error.code == CAPTCHA_ERROR_CODE:
                    error = Captcha(
                        self,
                        error.error['captcha_sid'],
                        self.method,
                        (method,),
                        {'values': values},
                        error.error['captcha_img']
                    )

                response = self.error_handlers[error.code](error)

                if response is not None:
                    return response

            raise error

        return response if raw else response['response']


class VkApiMethod(object):
    def __init__(self, vk, method=None):
        self._vk = vk
        self._method = method

    def __getattr__(self, method):
        if self._method:
            self._method += '.' + method
            return self

        return VkApiMethod(self._vk, method)

    def __call__(self, **kwargs):
        return self._vk.method(self._method, kwargs)
